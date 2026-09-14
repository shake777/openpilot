# 운행 중 K7 레이더 CAN을 C4 전용 서버로 자동 전송하는 서비스
import os
import signal
import threading
import time
from pathlib import Path

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from tools.c4_diagnostics.auto_upload import load_or_start_state, pending_captures, upload_one
from tools.c4_diagnostics.radar_capture import RadarCaptureWriter
from tools.c4_diagnostics.scene_capture import SceneCaptureWriter, build_scene_frame
from tools.c4_diagnostics.upload import UploadError, load_config


NetworkType = log.DeviceState.NetworkType
CONFIG_RETRY_SECONDS = 60
UPLOAD_RETRY_SECONDS = 15
SCENE_INTERVAL_SECONDS = 0.1


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


def finalize_capture(writer: RadarCaptureWriter, scene_writer: SceneCaptureWriter) -> None:
  scene = scene_writer.finalize()
  capture = writer.finalize()
  if capture is None:
    if scene is not None:
      scene.unlink(missing_ok=True)
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

  network_online = threading.Event()
  uploader = threading.Thread(
    target=upload_loop,
    args=(config, source_id, state, state_path, spool_dir, network_online, stop_event),
    daemon=True,
  )
  uploader.start()

  can_sock = messaging.sub_sock("can", conflate=False, timeout=1000)
  sm = messaging.SubMaster(["deviceState", "carState", "modelV2", "liveTracks", "radarState", "carControl"])
  writer = RadarCaptureWriter(spool_dir)
  scene_writer = SceneCaptureWriter(spool_dir, writer.capture_name, writer.started_at)
  next_scene_time = time.monotonic()
  try:
    while not stop_event.is_set():
      sm.update(0)
      if sm.valid["deviceState"] and sm["deviceState"].networkType != NetworkType.none:
        network_online.set()
      else:
        network_online.clear()

      message = messaging.recv_one_or_none(can_sock)
      if message is not None:
        for frame in message.can:
          writer.append(message.logMonoTime, frame.address, frame.src, bytes(frame.dat))

      now = time.time()
      monotonic_now = time.monotonic()
      if monotonic_now >= next_scene_time and any(sm.seen[name] for name in ("carState", "modelV2", "liveTracks", "radarState")):
        scene_writer.append(build_scene_frame(
          time.monotonic_ns(), sm["carState"], sm["modelV2"], sm["liveTracks"], sm["radarState"], sm["carControl"],
        ))
        next_scene_time = monotonic_now + SCENE_INTERVAL_SECONDS

      if writer.should_rotate(now) or scene_writer.should_rotate(now):
        finalize_capture(writer, scene_writer)
        writer = RadarCaptureWriter(spool_dir, now)
        scene_writer = SceneCaptureWriter(spool_dir, writer.capture_name, writer.started_at)
  finally:
    finalize_capture(writer, scene_writer)
    stop_event.set()
    uploader.join(timeout=5)


if __name__ == "__main__":
  main()
