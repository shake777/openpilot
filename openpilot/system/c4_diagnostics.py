# 운행 중 K7 레이더 CAN을 C4 전용 서버로 자동 전송하는 서비스
import os
import signal
import threading
import time
from collections import deque
from pathlib import Path

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from tools.c4_diagnostics.auto_upload import load_or_start_state, pending_captures, upload_one
from tools.c4_diagnostics.can_inventory import CanInventoryWriter
from tools.c4_diagnostics.parked_probe import summarize_last_once
from tools.c4_diagnostics.qcamera_capture import (
  QCameraCaptureWriter,
  next_representative_video_time,
  representative_video_due,
  representative_video_rollover_due,
)
from tools.c4_diagnostics.radar_capture import MAX_PENDING_DIAGNOSTICS, RadarCaptureWriter, capture_can_frame
from tools.c4_diagnostics.scene_capture import SceneCaptureWriter, build_scene_frame
from tools.c4_diagnostics.upload import UploadError, load_config


NetworkType = log.DeviceState.NetworkType
CONFIG_RETRY_SECONDS = 60
UPLOAD_RETRY_SECONDS = 15
SCENE_INTERVAL_SECONDS = 0.1
SCENE_SERVICES = ("carState", "modelV2", "liveTracks", "radarState", "carControl")


def read_source_id(config) -> str | None:
  if config.source_id:
    return config.source_id
  value = Params().get("DongleId")
  if isinstance(value, bytes):
    value = value.decode("utf-8", errors="replace")
  return value or None


def write_meminfo(capture: Path) -> None:
  source = Path("/proc/meminfo")
  if not source.is_file():
    return
  wanted = {"MemTotal", "MemAvailable", "CommitLimit", "Committed_AS", "CmaTotal", "CmaFree"}
  lines = [line for line in source.read_text(encoding="utf-8", errors="replace").splitlines()
           if line.partition(":")[0] in wanted]
  if lines:
    target = capture.with_suffix(".meminfo")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def finalize_capture(writer: RadarCaptureWriter, scene_writer: SceneCaptureWriter,
                     inventory_writer: CanInventoryWriter, video_writer: QCameraCaptureWriter | None) -> None:
  scene = scene_writer.finalize()
  inventory = inventory_writer.finalize()
  video, video_metadata = video_writer.finalize() if video_writer is not None else (None, None)
  capture = writer.finalize()
  if capture is None:
    if scene is not None:
      scene.unlink(missing_ok=True)
    if inventory is not None:
      inventory.unlink(missing_ok=True)
    if video is not None:
      video.unlink(missing_ok=True)
    if video_metadata is not None:
      video_metadata.unlink(missing_ok=True)
    return
  write_meminfo(capture)


def wait_for_config(stop_event: threading.Event):
  while not stop_event.is_set():
    try:
      config = load_config()
      source_id = read_source_id(config)
      if source_id:
        return config, source_id
      cloudlog.warning("C4 diagnostics source_id is not configured")
    except UploadError as exc:
      cloudlog.warning("C4 diagnostics is inactive: %s", exc)
    stop_event.wait(CONFIG_RETRY_SECONDS)
  return None, None


def upload_loop(config, source_id: str, state: dict, state_path: Path, spool_dir: Path,
                network_online: threading.Event, stop_event: threading.Event) -> None:
  while not stop_event.is_set():
    if not network_online.wait(UPLOAD_RETRY_SECONDS):
      continue
    captures = pending_captures(spool_dir, state)
    if not captures:
      stop_event.wait(UPLOAD_RETRY_SECONDS)
      continue
    try:
      result = upload_one(config, source_id, state, state_path, captures[0])
      cloudlog.event("c4_diagnostics_uploaded", upload_id=result["upload_id"], file=captures[0].name)
    except UploadError as exc:
      cloudlog.warning("C4 diagnostics upload failed: %s", exc)
      stop_event.wait(UPLOAD_RETRY_SECONDS)


def scene_ready(sm) -> bool:
  return all(sm.seen[name] for name in SCENE_SERVICES)


def main() -> None:
  stop_event = threading.Event()
  signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
  signal.signal(signal.SIGINT, lambda *_args: stop_event.set())
  config, source_id = wait_for_config(stop_event)
  if config is None or source_id is None:
    return

  spool_dir = Path(config.spool_dir)
  state_path = Path(config.state_path)
  state = load_or_start_state(state_path)

  try:
    summarize_last_once(spool_dir)
  except (OSError, ValueError) as exc:
    cloudlog.warning("C4 saved diagnostic summary failed: %s", exc)

  network_online = threading.Event()
  uploader = threading.Thread(
    target=upload_loop,
    args=(config, source_id, state, state_path, spool_dir, network_online, stop_event),
    daemon=True,
  )
  uploader.start()

  can_sock = messaging.sub_sock("can", conflate=False, timeout=1000)
  sendcan_sock = messaging.sub_sock("sendcan", conflate=False)
  qroad_sock = messaging.sub_sock("qRoadEncodeData", conflate=False)
  pending_diagnostics = deque(maxlen=MAX_PENDING_DIAGNOSTICS)
  sm = messaging.SubMaster(["deviceState", "carState", "modelV2", "liveTracks", "radarState", "carControl"])
  writer = None
  scene_writer = None
  inventory_writer = None
  video_writer = None
  next_video_time = None
  next_scene_time = time.monotonic()
  try:
    while not stop_event.is_set():
      sm.update(0)
      if sm.valid["deviceState"] and sm["deviceState"].networkType != NetworkType.none:
        network_online.set()
      else:
        network_online.clear()

      onroad = sm.valid["deviceState"] and sm["deviceState"].started
      monotonic_now = time.monotonic()
      if not onroad:
        next_video_time = None
      elif next_video_time is None:
        next_video_time = next_representative_video_time(monotonic_now)
      if (onroad and writer is not None and scene_writer is not None and inventory_writer is not None and
          representative_video_rollover_due(monotonic_now, next_video_time, video_writer is not None)):
        finalize_capture(writer, scene_writer, inventory_writer, video_writer)
        writer = None
        scene_writer = None
        inventory_writer = None
        video_writer = None
      if onroad and writer is None:
        writer = RadarCaptureWriter(spool_dir)
        scene_writer = SceneCaptureWriter(spool_dir, writer.capture_name, writer.started_at)
        inventory_writer = CanInventoryWriter(spool_dir, writer.capture_name)
        if representative_video_due(monotonic_now, next_video_time):
          video_writer = QCameraCaptureWriter(spool_dir, writer.capture_name)
          next_video_time = next_representative_video_time(monotonic_now)
        else:
          video_writer = None
        next_scene_time = monotonic_now
      elif not onroad and writer is not None and scene_writer is not None and inventory_writer is not None:
        finalize_capture(writer, scene_writer, inventory_writer, video_writer)
        writer = None
        scene_writer = None
        inventory_writer = None
        video_writer = None

      message = messaging.recv_one_or_none(can_sock)
      if message is not None:
        for frame in message.can:
          if inventory_writer is not None:
            inventory_writer.append(message.logMonoTime, frame.address, frame.src, bytes(frame.dat))
          capture_can_frame(writer, pending_diagnostics, message.logMonoTime, frame.address, frame.src, bytes(frame.dat))
      for sent in messaging.drain_sock(sendcan_sock, wait_for_one=False):
        for frame in sent.sendcan:
          if frame.address == 0x7D0:
            capture_can_frame(writer, pending_diagnostics, sent.logMonoTime, frame.address, frame.src, bytes(frame.dat))

      for encoded in messaging.drain_sock(qroad_sock, wait_for_one=False):
        if video_writer is not None and not video_writer.complete():
          video_writer.append(encoded.logMonoTime, encoded.qRoadEncodeData)

      now = time.time()
      if scene_writer is not None and monotonic_now >= next_scene_time and scene_ready(sm):
        scene_writer.append(build_scene_frame(
          time.monotonic_ns(), sm["carState"], sm["modelV2"], sm["liveTracks"], sm["radarState"], sm["carControl"],
        ))
        next_scene_time = monotonic_now + SCENE_INTERVAL_SECONDS

      if (writer is not None and scene_writer is not None and inventory_writer is not None and
          (writer.should_rotate(now) or scene_writer.should_rotate(now))):
        finalize_capture(writer, scene_writer, inventory_writer, video_writer)
        writer = None
        scene_writer = None
        inventory_writer = None
        video_writer = None
  finally:
    if writer is not None and scene_writer is not None and inventory_writer is not None:
      finalize_capture(writer, scene_writer, inventory_writer, video_writer)
    stop_event.set()
    uploader.join(timeout=5)


if __name__ == "__main__":
  main()
