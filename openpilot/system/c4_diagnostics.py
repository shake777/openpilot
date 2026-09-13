# 운행 중 K7 레이더 CAN을 48시간만 수집해 C4 전용 서버로 자동 전송하는 서비스
import os
import signal
import threading
import time
from pathlib import Path

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from tools.c4_diagnostics.auto_upload import is_active, load_or_start_state, pending_captures, upload_one
from tools.c4_diagnostics.radar_capture import RadarCaptureWriter
from tools.c4_diagnostics.upload import UploadError, load_config


NetworkType = log.DeviceState.NetworkType
CONFIG_RETRY_SECONDS = 60
UPLOAD_RETRY_SECONDS = 15


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
  while not stop_event.is_set() and is_active(state):
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
  state = load_or_start_state(state_path, config.duration_hours)
  if not is_active(state):
    cloudlog.event("c4_diagnostics_expired", expires_at=state["expires_at"])
    stop_event.wait()
    return

  network_online = threading.Event()
  uploader = threading.Thread(
    target=upload_loop,
    args=(config, source_id, state, state_path, spool_dir, network_online, stop_event),
    daemon=True,
  )
  uploader.start()

  can_sock = messaging.sub_sock("can", conflate=False, timeout=1000)
  sm = messaging.SubMaster(["deviceState"])
  writer = RadarCaptureWriter(spool_dir)
  try:
    while not stop_event.is_set() and is_active(state):
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
      if writer.should_rotate(now):
        capture = writer.finalize()
        if capture is not None:
          write_meminfo(capture)
        writer = RadarCaptureWriter(spool_dir, now)
  finally:
    capture = writer.finalize()
    if capture is not None:
      write_meminfo(capture)
    stop_event.set()
    uploader.join(timeout=5)


if __name__ == "__main__":
  main()
