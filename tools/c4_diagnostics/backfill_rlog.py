# 실시간 C4 진단이 놓친 주행을 C4에 남은 rlog/qcamera로 다시 만들어 업로드 대기열에 넣는 도구
"""Rebuild C4 diagnostic bundles from a recorded route.

The live service (openpilot/system/c4_diagnostics.py) writes bundles while driving. When it was not
running (e.g. it failed to start), this replays the route's rlog segments through the same capture
writers and the same schedule: a bundle with 30 s of qcamera video at the drive start and every
10 minutes, plus data-only event bundles. Bundles land in the spool, where the running service
uploads them. Run on the C4 with the openpilot venv python:

  /usr/local/venv/bin/python -m tools.c4_diagnostics.backfill_rlog /data/media/0/realdata/<route>--*
"""
import argparse
import json
import os
import subprocess
import sys
import uuid
from collections import deque
from pathlib import Path

from tools.c4_diagnostics.can_inventory import CanInventoryWriter
from tools.c4_diagnostics.event_capture import EVENT_POST_SECONDS, EventDetector, TimeRing, write_event_companion
from tools.c4_diagnostics.qcamera_capture import MAX_QCAMERA_BYTES, REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS, VIDEO_DURATION_SECONDS
from tools.c4_diagnostics.radar_capture import MAX_PENDING_DIAGNOSTICS, OtherRadarThrottle, RadarCaptureWriter, capture_can_frame
from tools.c4_diagnostics.scene_capture import SceneCaptureWriter, build_scene_frame, scene_nav

BACKFILL_NAMESPACE = uuid.UUID("5d0f1f2e-6c1b-4f53-9a51-0c4bacf111ed")
SCENE_INTERVAL_NS = 100_000_000
PERIODIC_BUNDLE_SECONDS = 60.0
SCENE_SERVICES = ("carState", "modelV2", "liveTracks", "radarState", "carControl")
TRACKED_SERVICES = (*SCENE_SERVICES, "deviceState", "longitudinalPlan", "liveCalibration", "roadCameraState",
                    "liveParameters", "liveDelay", "carrotMan", "carrotNavi", "navInstructionCarrot", "carParams")
SETTINGS_EVERY_FRAMES = 50


def segment_number(path: Path) -> int:
  return int(path.name.rsplit("--", 1)[1])


def capture_id(route: str, mono_ns: int) -> str:
  # Deterministic, so a second run recognises bundles it already made.
  return uuid.uuid5(BACKFILL_NAMESPACE, f"{route}:{mono_ns}").hex


def params_settings(entries: dict[str, bytes], keys) -> dict[str, float]:
  """The live service's Params.get_float values, from the initData params snapshot."""
  values = {}
  for key in keys:
    raw = entries.get(key)
    if raw is None:
      continue
    try:
      values[key] = float(raw.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
      continue
  return values


def read_events(rlog: Path):
  import zstandard
  from openpilot.cereal import log
  data = rlog.read_bytes()
  if rlog.suffix == ".zst":
    data = zstandard.ZstdDecompressor().stream_reader(data).read()
  events = list(log.Event.read_multiple_bytes(data))
  events.sort(key=lambda event: event.logMonoTime)
  return events


def extract_video(qcamera: Path, target: Path) -> bool:
  """First 30 s of the segment's qcamera.ts as Annex-B H.264 (the live bundle format)."""
  partial = target.with_name(target.name + ".partial")
  result = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(qcamera), "-t", str(VIDEO_DURATION_SECONDS),
                           "-c:v", "copy", "-an", "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(partial)],
                          capture_output=True, check=False)
  if result.returncode != 0 or not partial.is_file() or not 0 < partial.stat().st_size <= MAX_QCAMERA_BYTES:
    partial.unlink(missing_ok=True)
    return False
  os.replace(partial, target)
  os.chmod(target, 0o600)
  return True


def video_size(qcamera: Path) -> tuple[int, int]:
  result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(qcamera)], capture_output=True, text=True, check=False)
  try:
    width, height = (int(value) for value in result.stdout.strip().split(",")[:2])
    return width, height
  except ValueError:
    return 0, 0


class Backfill:
  def __init__(self, spool_dir: Path, route: str, setting_keys, scene_settings_extra, scene_camera):
    self.spool_dir = spool_dir
    self.route = route
    self.setting_keys = setting_keys
    self.scene_settings_extra = scene_settings_extra
    self.scene_camera = scene_camera
    self.latest = {}
    self.params = {}
    self.wall_offset_ns = None
    self.writer = self.scene_writer = self.inventory_writer = None
    self.bundle_end_ns = None
    self.bundle_event = None
    self.video = None
    self.next_periodic_ns = None
    self.next_scene_ns = None
    self.scene_count = 0
    self.can_ring = TimeRing()
    self.scene_ring = TimeRing()
    self.ring_throttle = OtherRadarThrottle()
    self.detector = EventDetector()
    self.pending_diagnostics = deque(maxlen=MAX_PENDING_DIAGNOSTICS)
    self.made: list[str] = []
    self.skipped = 0

  def wall(self, mono_ns: int) -> float:
    return (mono_ns + self.wall_offset_ns) / 1e9

  def _open(self, mono_ns: int, end_ns: int) -> bool:
    name_id = capture_id(self.route, mono_ns)
    if any(self.spool_dir.glob(f"*-{name_id}.c4radar*")):
      self.skipped += 1
      return False
    self.writer = RadarCaptureWriter(self.spool_dir, wall_time=self.wall(mono_ns), capture_id=name_id)
    self.scene_writer = SceneCaptureWriter(self.spool_dir, self.writer.capture_name, self.writer.started_at)
    self.inventory_writer = CanInventoryWriter(self.spool_dir, self.writer.capture_name)
    self.bundle_end_ns = end_ns
    self.bundle_event = None
    return True

  def _close(self) -> None:
    if self.writer is None:
      return
    self.scene_writer.finalize()
    self.inventory_writer.finalize()
    capture = self.writer.finalize()
    video, self.video = self.video, None
    if capture is not None:
      if self.bundle_event is not None:
        write_event_companion(capture, *self.bundle_event)
      if video is not None:
        qcamera, started_ns, frames, size = video
        target = capture.with_suffix(".qcamera.h264")
        if extract_video(qcamera, target):
          target.with_suffix(".json").write_text(json.dumps({
            "schema": "c4-qcamera-video-v1", "codec": "h264", "container": "annex-b", "started_mono_ns": started_ns,
            "duration_s": VIDEO_DURATION_SECONDS, "frames": frames, "width": size[0], "height": size[1],
          }, separators=(",", ":")) + "\n", encoding="utf-8")
          os.chmod(target.with_suffix(".json"), 0o600)
      self.made.append(capture.name)
    self.writer = self.scene_writer = self.inventory_writer = None

  def start_segment(self, segment: Path, events) -> None:
    """A periodic bundle starts at the first segment after each 10-minute mark (video aligned to the segment)."""
    first_ns = events[0].logMonoTime
    if self.next_periodic_ns is not None and first_ns < self.next_periodic_ns:
      return
    if self.writer is not None and self.bundle_event is not None:
      return  # an event bundle is still running; the next segment takes the periodic slot
    self._close()
    if not self._open(first_ns, first_ns + int(PERIODIC_BUNDLE_SECONDS * 1e9)):
      self.next_periodic_ns = first_ns + int(REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS * 1e9)
      return
    self.next_periodic_ns = first_ns + int(REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS * 1e9)
    frames = [event for event in events if event.which() == "qRoadEncodeIdx"]
    qcamera = segment / "qcamera.ts"
    if frames and qcamera.is_file():
      start = frames[0].logMonoTime
      count = sum(1 for event in frames if event.logMonoTime - start < VIDEO_DURATION_SECONDS * 1e9)
      self.video = (qcamera, start, count, video_size(qcamera))

  def handle(self, event) -> None:
    kind = event.which()
    mono = event.logMonoTime
    if kind == "initData":
      init = event.initData
      self.wall_offset_ns = int(init.wallTimeNanos) - mono
      self.params = {entry.key: bytes(entry.value) for entry in init.params.entries}
      return
    if self.wall_offset_ns is None:
      return
    if kind in TRACKED_SERVICES:
      self.latest[kind] = getattr(event, kind)
    if self.writer is not None and mono >= self.bundle_end_ns:
      self._close()
    if kind == "can":
      for frame in event.can:
        data = bytes(frame.dat)
        if self.ring_throttle.allow(mono, frame.address, frame.src, data):
          self.can_ring.push(mono, (mono, frame.address, frame.src, data))
        if self.inventory_writer is not None:
          self.inventory_writer.append(mono, frame.address, frame.src, data)
        capture_can_frame(self.writer, self.pending_diagnostics, mono, frame.address, frame.src, data)
    elif kind == "sendcan":
      for frame in event.sendcan:
        if frame.address == 0x7D0:
          capture_can_frame(self.writer, self.pending_diagnostics, mono, frame.address, frame.src, bytes(frame.dat))
    if all(name in self.latest for name in SCENE_SERVICES) and (self.next_scene_ns is None or mono >= self.next_scene_ns):
      self._scene(mono)

  def _scene(self, mono: int) -> None:
    latest = self.latest
    settings = None
    if self.scene_count % SETTINGS_EVERY_FRAMES == 0:
      settings = params_settings(self.params, self.setting_keys)
      settings.update(self.scene_settings_extra(latest))
    frame = build_scene_frame(
      mono, latest["carState"], latest["modelV2"], latest["liveTracks"], latest["radarState"], latest["carControl"],
      latest.get("longitudinalPlan"), self.scene_camera(latest), settings,
      scene_nav(latest.get("carrotMan"), latest["carState"], latest.get("carrotNavi"), latest.get("navInstructionCarrot")))
    self.scene_count += 1
    self.next_scene_ns = mono + SCENE_INTERVAL_NS
    if self.scene_writer is not None:
      self.scene_writer.append(frame)
    else:
      self.scene_ring.push(mono, frame)
    car_state, car_control = latest["carState"], latest["carControl"]
    reason = self.detector.update(mono, car_state.vEgo, car_state.brakePressed, car_control.longActive,
                                  car_control.actuators.accel, latest["radarState"].leadOne.status)
    if reason is not None and self.writer is not None:
      self.bundle_event = self.bundle_event or (reason, mono)
    elif reason is not None:
      if not self._open(mono, mono + int(EVENT_POST_SECONDS * 1e9)):
        return
      for record in self.can_ring.drain():
        self.writer.append(*record)
      for buffered in self.scene_ring.drain():
        self.scene_writer.append(buffered)
      self.bundle_event = (reason, mono)

  def finish(self) -> None:
    self._close()


def live_helpers():
  """The live service's settings keys, learned-value and camera fields, from log messages."""
  from openpilot.common.transformations.camera import DEVICE_CAMERAS
  from openpilot.system.c4_diagnostics import DEFAULT_CAMERA_HEIGHT_M, SCENE_SETTING_KEYS

  def extra(latest):
    values = {}
    car_params = latest.get("carParams")
    if car_params is not None:
      values.update({"Wheelbase": float(car_params.wheelbase),
                     "OpenpilotLongitudinal": float(car_params.openpilotLongitudinalControl),
                     "PcmCruise": float(car_params.pcmCruise)})
    lp = latest.get("liveParameters")
    if lp is not None:
      values.update({"LearnedSteerRatio": float(lp.steerRatio), "LearnedSteerRatioValid": float(lp.steerRatioValid),
                     "LearnedStiffness": float(lp.stiffnessFactor), "LearnedAngleOffsetDeg": float(lp.angleOffsetAverageDeg)})
    ld = latest.get("liveDelay")
    if ld is not None:
      values.update({"LearnedLatDelay": float(ld.lateralDelayEstimate),
                     "LearnedLatDelayValid": float(str(ld.status) == "estimated")})
    return values

  def camera(latest):
    result = {}
    calib = latest.get("liveCalibration")
    if calib is not None and len(calib.rpyCalib) == 3:
      height = float(calib.height[0]) if len(calib.height) else DEFAULT_CAMERA_HEIGHT_M
      result["calib"] = [*list(calib.rpyCalib), height]
    road, device = latest.get("roadCameraState"), latest.get("deviceState")
    if road is not None and device is not None:
      config = DEVICE_CAMERAS.get((str(device.deviceType), str(road.sensor)))
      if config is not None:
        result["cam"] = [config.fcam.width, config.fcam.height, config.fcam.focal_length]
    return result

  return SCENE_SETTING_KEYS, extra, camera


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("segments", nargs="+", type=Path, help="segment directories of one route (<route>--N)")
  parser.add_argument("--spool", type=Path, default=None, help="spool directory (default: C4 config)")
  args = parser.parse_args(argv)
  segments = sorted((path for path in args.segments if (path / "rlog.zst").is_file() or (path / "rlog").is_file()),
                    key=segment_number)
  if not segments:
    print("no segments with rlog", file=sys.stderr)
    return 1
  routes = {path.name.rsplit("--", 1)[0] for path in segments}
  if len(routes) != 1:
    print(f"segments must belong to one route: {sorted(routes)}", file=sys.stderr)
    return 1
  if args.spool is None:
    from tools.c4_diagnostics.upload import load_config
    args.spool = Path(load_config().spool_dir)
  keys, extra, camera = live_helpers()
  backfill = Backfill(args.spool, routes.pop(), keys, extra, camera)
  for segment in segments:
    rlog = segment / "rlog.zst" if (segment / "rlog.zst").is_file() else segment / "rlog"
    try:
      events = read_events(rlog)
    except Exception as exc:  # a truncated last segment must not stop the rest
      print(f"{segment.name}: unreadable rlog ({exc})", file=sys.stderr)
      continue
    if not events:
      continue
    for event in events:
      if event.which() == "initData":
        backfill.handle(event)
    backfill.start_segment(segment, events)
    for event in events:
      if event.which() != "initData":
        backfill.handle(event)
    print(f"{segment.name}: {len(events)} events, bundles so far {len(backfill.made)}", flush=True)
  backfill.finish()
  print(f"made {len(backfill.made)} bundles, skipped {backfill.skipped} already present")
  return 0


if __name__ == "__main__":
  sys.exit(main())
