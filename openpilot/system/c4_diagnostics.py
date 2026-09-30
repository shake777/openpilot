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
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from tools.c4_diagnostics.auto_upload import load_or_start_state, pending_captures, prune_spool, upload_one
from tools.c4_diagnostics.can_inventory import CanInventoryWriter
from tools.c4_diagnostics.parked_probe import summarize_last_once
from tools.c4_diagnostics.qcamera_capture import (
  QCameraCaptureWriter,
  plan_bundle_start,
)
from tools.c4_diagnostics.event_capture import EVENT_POST_SECONDS, EventDetector, TimeRing, write_event_companion
from tools.c4_diagnostics.radar_capture import MAX_PENDING_DIAGNOSTICS, RadarCaptureWriter, capture_can_frame, is_radar_address
from tools.c4_diagnostics.scene_capture import SceneCaptureWriter, build_scene_frame
from tools.c4_diagnostics.upload import UploadError, load_config


NetworkType = log.DeviceState.NetworkType
CONFIG_RETRY_SECONDS = 60
UPLOAD_RETRY_SECONDS = 15
SCENE_INTERVAL_SECONDS = 0.1
# Video bundles keep the existing ~40-60 s window. The data-only bundle between
# them is limited to 20 s (~1.8 MB) to keep server load low.
VIDEO_BUNDLE_MAX_SECONDS = 60.0
DATA_BUNDLE_MAX_SECONDS = 20.0
SCENE_SERVICES = ("carState", "modelV2", "liveTracks", "radarState", "carControl")
# Optional: scenes are still written before the first plan arrives.
PLAN_SERVICE = "longitudinalPlan"
CAMERA_SERVICES = ("liveCalibration", "roadCameraState")
# Lateral settings recorded for the server's lane-centering recommendations.
SCENE_SETTING_KEYS = ("UseLaneLineSpeed", "PathOffset", "CameraYawTrimDeg", "AdjustLaneOffset", "SteerActuatorDelay",
                      "LatSmoothSec", "CustomSR", "LatMpcPathCost", "LatMpcInputOffset")
SCENE_SETTINGS_EVERY_FRAMES = 50


def scene_settings(params):
  values = {}
  for key in SCENE_SETTING_KEYS:
    try:
      values[key] = params.get_float(key)
    except Exception:
      continue
  return values
DEFAULT_CAMERA_HEIGHT_M = 1.22


def scene_camera(sm):
  """Road-camera calibration and intrinsics for the server-side video overlay."""
  camera = {}
  if sm.seen["liveCalibration"]:
    calib = sm["liveCalibration"]
    rpy = list(calib.rpyCalib)
    if len(rpy) == 3:
      height = float(calib.height[0]) if len(calib.height) else DEFAULT_CAMERA_HEIGHT_M
      camera["calib"] = [*rpy, height]
  if sm.seen["roadCameraState"] and sm.seen["deviceState"]:
    config = DEVICE_CAMERAS.get((str(sm["deviceState"].deviceType), str(sm["roadCameraState"].sensor)))
    if config is not None:
      camera["cam"] = [config.fcam.width, config.fcam.height, config.fcam.focal_length]
  return camera


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
                     inventory_writer: CanInventoryWriter, video_writer: QCameraCaptureWriter | None,
                     event: tuple[str, int] | None = None) -> None:
  scene = scene_writer.finalize()
  inventory = inventory_writer.finalize()
  video, video_metadata = video_writer.finalize() if video_writer is not None else (None, None)
  capture = writer.finalize()
  if capture is not None and event is not None:
    write_event_companion(capture, *event)
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
    try:
      prune_spool(spool_dir, state)
    except OSError as exc:
      cloudlog.warning("C4 diagnostics spool cleanup failed: %s", exc)
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
  sm = messaging.SubMaster(["deviceState", "carState", "modelV2", "liveTracks", "radarState", "carControl", PLAN_SERVICE,
                            *CAMERA_SERVICES])
  writer = None
  scene_writer = None
  inventory_writer = None
  video_writer = None
  next_capture_time = None
  next_video_time = None
  bundle_max_age = VIDEO_BUNDLE_MAX_SECONDS
  bundle_event = None
  # Last 15 s of radar CAN and scene frames, written first when an event starts a bundle.
  can_ring = TimeRing()
  scene_ring = TimeRing()
  detector = EventDetector()
  params = Params()
  scene_count = 0
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
        next_capture_time = None
        next_video_time = None
        can_ring.drain()
        scene_ring.drain()
      elif writer is None:
        # A video bundle at drive start and every 10 minutes; events add data-only bundles.
        start, with_video, next_capture_time, next_video_time = plan_bundle_start(
          monotonic_now, next_capture_time, next_video_time)
        if start:
          writer = RadarCaptureWriter(spool_dir)
          scene_writer = SceneCaptureWriter(spool_dir, writer.capture_name, writer.started_at)
          inventory_writer = CanInventoryWriter(spool_dir, writer.capture_name)
          video_writer = QCameraCaptureWriter(spool_dir, writer.capture_name) if with_video else None
          bundle_max_age = VIDEO_BUNDLE_MAX_SECONDS if with_video else DATA_BUNDLE_MAX_SECONDS
          bundle_event = None
          next_scene_time = monotonic_now
      if not onroad and writer is not None and scene_writer is not None and inventory_writer is not None:
        finalize_capture(writer, scene_writer, inventory_writer, video_writer, bundle_event)
        writer = None
        scene_writer = None
        inventory_writer = None
        video_writer = None

      message = messaging.recv_one_or_none(can_sock)
      if message is not None:
        for frame in message.can:
          if onroad and is_radar_address(frame.address, frame.src):
            can_ring.push(message.logMonoTime, (message.logMonoTime, frame.address, frame.src, bytes(frame.dat)))
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
      if onroad and monotonic_now >= next_scene_time and scene_ready(sm):
        scene_mono = time.monotonic_ns()
        scene_frame = build_scene_frame(
          scene_mono, sm["carState"], sm["modelV2"], sm["liveTracks"], sm["radarState"], sm["carControl"],
          sm[PLAN_SERVICE] if sm.seen[PLAN_SERVICE] else None, scene_camera(sm),
          scene_settings(params) if scene_count % SCENE_SETTINGS_EVERY_FRAMES == 0 else None,
        )
        scene_count += 1
        next_scene_time = monotonic_now + SCENE_INTERVAL_SECONDS
        if scene_writer is not None:
          scene_writer.append(scene_frame)
        else:
          scene_ring.push(scene_mono, scene_frame)
        car_state, car_control = sm["carState"], sm["carControl"]
        reason = detector.update(scene_mono, car_state.vEgo, car_state.brakePressed, car_control.longActive,
                                 car_control.actuators.accel, sm["radarState"].leadOne.status)
        if reason is not None and writer is not None:
          bundle_event = bundle_event or (reason, scene_mono)
        elif reason is not None:
          # Data-only event bundle: the buffered 15 s first, then 5 s more.
          writer = RadarCaptureWriter(spool_dir)
          scene_writer = SceneCaptureWriter(spool_dir, writer.capture_name, writer.started_at)
          inventory_writer = CanInventoryWriter(spool_dir, writer.capture_name)
          video_writer = None
          for record in can_ring.drain():
            writer.append(*record)
          for frame in scene_ring.drain():
            scene_writer.append(frame)
          bundle_max_age = EVENT_POST_SECONDS
          bundle_event = (reason, scene_mono)
          cloudlog.event("c4_diagnostics_event", reason=reason)

      if (writer is not None and scene_writer is not None and inventory_writer is not None and
          (writer.should_rotate(now, bundle_max_age) or scene_writer.should_rotate(now, bundle_max_age))):
        finalize_capture(writer, scene_writer, inventory_writer, video_writer, bundle_event)
        bundle_event = None
        writer = None
        scene_writer = None
        inventory_writer = None
        video_writer = None
  finally:
    if writer is not None and scene_writer is not None and inventory_writer is not None:
      finalize_capture(writer, scene_writer, inventory_writer, video_writer, bundle_event)
    stop_event.set()
    uploader.join(timeout=5)


if __name__ == "__main__":
  main()
