# 차량 식별 단계에서 정차 조건을 확인하고 K7 레이더 정보를 한 번만 읽는 모듈
import json
import math
import os
import time
import uuid
from pathlib import Path

from tools.c4_diagnostics.upload import UploadError, load_config


K7_PLATFORMS = frozenset(("KIA_K7", "KIA_K7_PE", "KIA_K7_HEV", "KIA_K7_HEV_PE"))
REQUESTS = (b"\x22\xf1\x00", b"\x22\x01\x42")
WAIT_SECONDS = 3.0
STATIONARY_SECONDS = 1.0
QUERY_SECONDS = 0.5
MAX_RAW_FRAMES = 64


class InventoryAborted(Exception):
  pass


def stationary_reason(cs, pandas, *, can_age, panda_age, panda_valid, controls_ready):
  if controls_ready:
    return "controls_already_ready"
  if not panda_valid or panda_age > 1.0 or not pandas:
    return "panda_state_unavailable"
  if len(pandas) != 1:
    return "multiple_pandas_not_supported"
  panda = pandas[0]
  if str(panda.safetyModel) != "elm327" or panda.controlsAllowed:
    return "not_in_inactive_firmware_query_stage"
  if str(panda.harnessStatus) not in ("normal", "flipped"):
    return "harness_not_connected"
  if not (panda.ignitionLine or panda.ignitionCan):
    return "ignition_off"
  if cs is None or can_age > 0.15 or not cs.canValid or cs.canTimeout:
    return "fresh_vehicle_state_unavailable"
  speeds = (cs.vEgo, cs.vEgoRaw, cs.wheelSpeeds.fl, cs.wheelSpeeds.fr, cs.wheelSpeeds.rl, cs.wheelSpeeds.rr)
  if not all(math.isfinite(v) and abs(v) <= 0.01 for v in speeds) or not cs.standstill:
    return "vehicle_not_stationary"
  if str(cs.gearShifter) != "park":
    return "gear_not_park"
  if cs.cruiseState.enabled or cs.gasPressed:
    return "cruise_or_accelerator_active"
  return None


def report_speed(value):
  return value if math.isfinite(value) else None


def allowed_read_frame(frame, request):
  data = bytes(frame.dat)
  return (frame.address == 0x7d0 and frame.src == 0 and
          data in ((b"\x03" + request).ljust(8, b"\x00"), b"\x30\x00\x0a\x00\x00\x00\x00\x00"))


def collect_inventory(can_recv, can_send, ready, query_factory, report):
  """Only two fixed reads and ISO-TP receive flow control; never change sessions."""
  report.update(status="running", f100={"status": "not_read"}, did_0142={"status": "not_read"}, raw_frames=[])

  def guard():
    reason = ready()
    if reason is not None:
      raise InventoryAborted(reason)

  def record(direction, frame):
    if frame.address in (0x7d0, 0x7d8) and len(report["raw_frames"]) < MAX_RAW_FRAMES:
      report["raw_frames"].append({"direction": direction, "address": hex(frame.address), "bus": frame.src,
                                   "data": bytes(frame.dat).hex(), "monotonic_ns": time.monotonic_ns()})

  for request, field in zip(REQUESTS, ("f100", "did_0142"), strict=True):
    nrc = None
    tx_blocked = False

    def receive(wait_for_one=False):
      nonlocal nrc, tx_blocked
      packets = can_recv(wait_for_one)
      for packet in packets:
        for frame in packet:
          record("rx", frame)
          data = bytes(frame.dat)
          if frame.address == 0x7d0 and frame.src == 192:
            tx_blocked = True
          if frame.address == 0x7d8 and frame.src == 0 and len(data) >= 4 and data[:3] == b"\x03\x7f\x22":
            if data[3] != 0x78:
              nrc = data[3]
      guard()
      return packets

    def send(frames):
      guard()
      if not all(allowed_read_frame(frame, request) for frame in frames):
        raise InventoryAborted("non_allowlisted_transmission_blocked")
      for frame in frames:
        record("tx", frame)
      can_send(frames)

    try:
      guard()
      query = query_factory(send, receive, 0, [0x7d0], [request], [b"\x62" + request[1:]],
                            response_pending_timeout=QUERY_SECONDS)
      result = query.get_data(QUERY_SECONDS, total_timeout=QUERY_SECONDS)
      guard()
      data = result.get((0x7d0, None))
      if data:
        report[field] = {"status": "ok", "raw_hex": data.hex()}
        if field == "f100":
          report[field]["ascii"] = "".join(chr(v) if 32 <= v < 127 else "." for v in data)
        continue
      report[field] = {"status": "tx_blocked" if tx_blocked else "rejected" if nrc is not None else "no_positive_response"}
      if nrc is not None:
        report[field]["nrc"] = f"0x{nrc:02x}"
    except InventoryAborted as exc:
      report[field] = {"status": "aborted", "reason": str(exc)}
    except Exception as exc:
      report[field] = {"status": "error", "error_type": type(exc).__name__}
    report["status"] = "partial" if report["f100"]["status"] == "ok" else report[field]["status"]
    return
  report["status"] = "complete"


def save_report(path, report):
  temporary = path.with_suffix(".tmp")
  with temporary.open("w", encoding="utf-8") as stream:
    os.chmod(temporary, 0o600)
    json.dump(report, stream, ensure_ascii=True, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
  os.replace(temporary, path)


def run_startup_inventory(ci, sm, params, can_recv, can_send):
  """Called before FirmwareQueryDone, CarParams publication and CI.init()."""
  if str(ci.CP.carFingerprint) not in K7_PLATFORMS or "REPLAY" in os.environ:
    return
  try:
    config = load_config()
  except UploadError:
    return
  boot_id = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
  spool = Path(config.spool_dir)
  spool.mkdir(parents=True, exist_ok=True)
  report_path = spool / f"radar-inventory-{boot_id}.json"
  report = {"schema": "c4-radar-inventory-v1", "created_at": time.time(), "car_fingerprint": str(ci.CP.carFingerprint),
            "status": "skipped", "write_performed": False, "session_changed": False, "safety_mode_changed": False,
            "known_activation_support": "unknown", "bus": 0, "request_address": "0x7d0", "response_address": "0x7d8"}
  try:
    claim = os.open(report_path.with_suffix(".claim"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
  except FileExistsError:
    if not report_path.exists():
      report.update(status="interrupted", reason="previous_attempt_did_not_finish_no_retry_this_boot")
      save_report(report_path, report)
    return
  os.close(claim)
  cs = None
  wheel_at = panda_at = float("-inf")

  def receive(wait_for_one=False):
    nonlocal cs, wheel_at
    packets = can_recv(wait_for_one)
    now = time.monotonic()
    if any(f.src == 0 and f.address == 0x386 for packet in packets for f in packet):
      wheel_at = now
    if packets:
      cs = ci.update([(time.monotonic_ns(), packet) for packet in packets])
    return packets

  def ready():
    nonlocal panda_at
    sm.update(0)
    now = time.monotonic()
    if sm.updated["pandaStates"]:
      panda_at = now
    if params.get_bool("FirmwareQueryDone") or params.get_int("EnableRadarTracks") != 0:
      return "startup_stage_or_track_setting_changed"
    return stationary_reason(cs, sm["pandaStates"], can_age=now - wheel_at, panda_age=now - panda_at,
                             panda_valid=sm.valid["pandaStates"], controls_ready=params.get_bool("ControlsReady"))

  try:
    from opendbc.car.isotp_parallel_query import IsoTpParallelQuery

    deadline = time.monotonic() + WAIT_SECONDS
    stationary_since = None
    reason = "stationary_window_not_observed"
    while time.monotonic() < deadline:
      receive(True)
      reason = ready()
      if reason is None:
        if stationary_since is None:
          stationary_since = time.monotonic()
        if time.monotonic() - stationary_since >= STATIONARY_SECONDS:
          collect_inventory(receive, can_send, ready, IsoTpParallelQuery, report)
          break
      else:
        stationary_since = None
        # Gear decoding can be unknown for the first CAN frames. Keep waiting for a stable P state,
        # but abort immediately if the vehicle moves or control/acceleration becomes active.
        if reason in ("vehicle_not_stationary", "cruise_or_accelerator_active",
                      "controls_already_ready", "startup_stage_or_track_setting_changed"):
          break
    else:
      reason = reason or "stationary_window_too_short"
    if report["status"] == "skipped":
      report["reason"] = reason
      if cs is not None and reason in ("vehicle_not_stationary", "gear_not_park"):
        report["stationary_evidence"] = {
          "gear": str(cs.gearShifter), "standstill": bool(cs.standstill),
          "v_ego": report_speed(cs.vEgo), "v_ego_raw": report_speed(cs.vEgoRaw),
          "wheel_speeds": {"fl": report_speed(cs.wheelSpeeds.fl), "fr": report_speed(cs.wheelSpeeds.fr),
                           "rl": report_speed(cs.wheelSpeeds.rl), "rr": report_speed(cs.wheelSpeeds.rr)},
        }
  except Exception as exc:
    report.update(status="error", error_type=type(exc).__name__)
  finally:
    save_report(report_path, report)
