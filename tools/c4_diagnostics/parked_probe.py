# 6999 터미널에서 K7 레이더 무쓰기 진단을 별도 일회성 서비스로 실행한다.
"""Run the existing parked K7 probe outside the comma service cgroup."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PROBE = ROOT / "openpilot/selfdrive/debug/car/hyundai_enable_radar_points.py"
CANDIDATE_TRIAL = ROOT / "tools/c4_diagnostics/k7_candidate_trial.py"
RESULT_DIR = Path("/data/c4-diagnostics")
STATUS_FILE = RESULT_DIR / "k7-parked-probe-status.json"
LOG_FILE = RESULT_DIR / "k7-parked-probe.log"
MAX_UPLOADED_LOG_BYTES = 32 * 1024


def run_command(command, **kwargs):
  return subprocess.run(command, check=False, text=True, **kwargs)


def vehicle_ready_for_probe(evidence=None):
  from openpilot.cereal import messaging

  sm = messaging.SubMaster(["carState", "deviceState"], poll="deviceState")
  evidence = evidence if evidence is not None else {}
  for attempt in range(10):
    sm.update(1000)
    reasons = []
    evidence.clear()
    evidence.update(attempt=attempt + 1, services={
      name: {"alive": bool(sm.alive[name]), "valid": bool(sm.valid[name])}
      for name in ("carState", "deviceState")})
    for name, state in evidence["services"].items():
      if not state["alive"] or not state["valid"]:
        reasons.append(f"{name} unavailable or invalid")
    if not reasons:
      car = sm["carState"]
      gear = str(car.gearShifter).lower().split(".")[-1]
      evidence.update(gear=gear, v_ego=float(car.vEgo), engine_rpm=float(car.engineRpm),
                      started=bool(sm["deviceState"].started))
      if gear != "park":
        reasons.append("gear is not P")
      if not abs(car.vEgo) < 0.1:
        reasons.append("vehicle is not stationary")
      if not car.engineRpm < 1:
        reasons.append("engine RPM is not zero")
      if not evidence["started"]:
        reasons.append("ignition/onroad state is not ready")
    evidence["reasons"] = reasons
    if not reasons:
      return True
  return False


def record_preflight_block(evidence, characterize, compare_sessions, candidate_trial=False):
  status = {"started_at": time.time(), "finished_at": time.time(), "probe_started": False,
            "comma_restarted": False, "restore_status": "not_needed", "final_config_verified": None,
            "error": "parked_state_unverified", "preflight": evidence,
            "mode": "candidate_trial" if candidate_trial else "characterize" if characterize else "security_probe",
            "compare_sessions": compare_sessions}
  RESULT_DIR.mkdir(parents=True, exist_ok=True)
  STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
  spool = probe_report_path().parent
  spool.mkdir(parents=True, exist_ok=True)
  path = spool / f"k7-security-probe-status-{uuid.uuid4()}.json"
  path.write_text(json.dumps({"schema": "c4-k7-parked-probe-status-v1", "created_at": time.time(),
                              "worker_status": status, "log_tail": "Preflight blocked; no diagnostic started."}) + "\n",
                  encoding="utf-8")
  os.chmod(path, 0o644)
  print(f"Blocked result queued for automatic upload: {path}")


def start_from_web(characterize=False, from_recovery=False, compare_sessions=False, candidate_trial=False):
  if not Path("/AGNOS").exists() or not PROBE.is_file() or (candidate_trial and not CANDIDATE_TRIAL.is_file()):
    print("C4 AGNOS or radar probe script not found; nothing was started")
    return 2
  if from_recovery or candidate_trial:
    evidence = {}
    print("Checking live parked state (up to 10 samples)...", flush=True)
    if not vehicle_ready_for_probe(evidence):
      print("Parked vehicle state could not be verified; nothing was started")
      print(json.dumps(evidence, ensure_ascii=False))
      try:
        record_preflight_block(evidence, characterize, compare_sessions, candidate_trial)
      except OSError as error:
        print(f"Could not save blocked result: {error}")
      return 2
  elif input("Parked, parking brake set, engine OFF and ignition ON? Type PARKED to start: ").strip() != "PARKED":
    print("Cancelled; nothing was started")
    return 2

  unit = "c4-k7-parked-probe"
  command = ["sudo", "-n", "systemd-run", "--collect", f"--unit={unit}",
             f"--working-directory={ROOT}", "--", sys.executable, str(Path(__file__).resolve()), "--worker"]
  if from_recovery or candidate_trial:
    command.append('--verify-parked')
  if characterize:
    command.append('--characterize')
  if compare_sessions:
    command.append('--compare-sessions')
  if candidate_trial:
    command.append('--candidate-trial')
  result = run_command(command)
  if result.returncode:
    print("Could not start isolated probe unit; comma service was not stopped")
    return result.returncode
  print(f"Started {unit}. The 6999 page may disconnect; wait for it to return.")
  print(f"Then read {STATUS_FILE} and {LOG_FILE}. Do not drive until the results are reviewed.")
  return 0


def wait_for_pandad(timeout_s=20):
  deadline = time.monotonic() + timeout_s
  while time.monotonic() < deadline:
    if run_command(["pidof", "pandad"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 1:
      return True
    time.sleep(0.5)
  return False


def probe_report_path():
  from tools.c4_diagnostics.upload import UploadError, load_config

  try:
    spool = Path(load_config().spool_dir)
  except UploadError:
    spool = RESULT_DIR / "spool"
  return spool / f"k7-security-probe-{uuid.uuid4()}.json"


def probe_command(report_path, characterize=False, compare_sessions=False, candidate_trial=False, restore_only=False):
  if candidate_trial:
    command = ["runuser", "-u", "comma", "--", "/usr/bin/env",
               f"PYTHONPATH={ROOT}:/data/pythonpath", sys.executable, str(CANDIDATE_TRIAL),
               "--output", str(report_path)]
    if restore_only:
      command.append('--restore-only')
    return command
  command = ["runuser", "-u", "comma", "--", "/usr/bin/env",
          f"PYTHONPATH={ROOT}:/data/pythonpath", sys.executable, str(PROBE),
          "--k7-characterize" if characterize else "--k7-security-probe", "--probe-output", str(report_path)]
  if compare_sessions:
    command.append('--compare-sessions')
  return command


def summarize_report(report):
  reads = report.get("reads", {})
  did_values = {}
  for name, entry in reads.items():
    if not name.startswith("did_"):
      continue
    extended = reads.get(f"extended_{name}", {})
    did_values[f"0x{name[4:]}"] = {
      "default": {key: entry[key] for key in ("status", "raw_hex", "nrc", "timeout") if key in entry},
      "extended": {key: extended[key] for key in ("status", "raw_hex", "nrc", "timeout") if key in extended},
    }
  configs = [report.get(key) for key in ("default_config_hex", "extended_config_hex",
                                         "post_config_hex", "post_restore_config_hex")]
  security = ({key: report[key] for key in ("status", "seed_length", "session_confirmation_source",
                                               "session_keepalive_confirmed", "key_sent", "write_performed") if key in report}
              if "security_level" in report else {})
  if security:
    security["f186_unsupported"] = any(error.get("stage") == "session_read" and error.get("nrc") == "0x31"
                                         for error in report.get("errors", []))
    security["configuration_unchanged_verified"] = (len(set(configs)) == 1 if all(configs) and report.get("final_config_verified") else None)
  return {"error_counts": dict(Counter(error.get("nrc", error.get("error_type", "unknown"))
                                       for error in report.get("errors", []))),
          "error_stages": [error.get("stage", "unknown") for error in report.get("errors", [])],
          "did_values": did_values, "security": security}


def summarize_last(spool_dir=None):
  # Only read saved evidence; this command never starts services or contacts the ECU.
  try:
    status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    if not status.get("report_path"):
      print("No saved diagnostic report; previous attempt may have been blocked")
      return 2
    report = json.loads(Path(status["report_path"]).read_text(encoding="utf-8"))
    status["summary"] = summarize_report(report)
    spool = Path(spool_dir) if spool_dir is not None else probe_report_path().parent
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / f"k7-security-probe-status-{uuid.uuid4()}.json"
    path.write_text(json.dumps({"schema": "c4-k7-parked-probe-status-v1", "created_at": time.time(),
                                "worker_status": status, "log_tail": "Summary from saved report; no new ECU requests."}) + "\n",
                    encoding="utf-8")
    os.chmod(path, 0o644)
    print(f"Saved result status: {status.get('report_status', 'unknown')}")
    print(f"Error counts: {status['summary']['error_counts']}")
    for did, values in status["summary"]["did_values"].items():
      if any(value.get("status") == "ok" for value in values.values()):
        print(f"{did}: default={values['default'].get('raw_hex', 'unavailable')} "
              f"extended={values['extended'].get('raw_hex', 'unavailable')}")
    print(f"Summary queued for automatic upload: {path}")
    return 0
  except (OSError, ValueError) as error:
    print(f"Could not summarize saved report: {error}")
    return 2


def summarize_last_once(spool_dir):
  marker = RESULT_DIR / "saved-summary-v1.done"
  if marker.exists():
    return 0
  if not STATUS_FILE.is_file():
    return 2
  result = summarize_last(spool_dir)
  if result == 0:
    marker.write_text("Saved report summary queued; server receipt is separate.\n", encoding="utf-8")
  return result


def run_worker(characterize=False, verify_parked=False, compare_sessions=False, candidate_trial=False):
  RESULT_DIR.mkdir(parents=True, exist_ok=True)
  status = {"started_at": time.time(), "probe_started": False, "probe_returncode": None,
            "report_path": None, "restore_status": None, "final_config_verified": None,
            "comma_restarted": False, "error": None,
            "mode": "candidate_trial" if candidate_trial else "characterize" if characterize else "security_probe"}
  stop_attempted = False
  with LOG_FILE.open("w", encoding="utf-8") as log:
    try:
      if verify_parked:
        status["preflight"] = {}
        if not vehicle_ready_for_probe(status["preflight"]):
          status["restore_status"] = "not_needed"
          raise RuntimeError("parked vehicle state changed; radar probe was not run")
      stop_attempted = True
      stop = run_command(["systemctl", "stop", "comma"], stdout=log, stderr=subprocess.STDOUT)
      if stop.returncode:
        raise RuntimeError(f"systemctl stop comma failed: {stop.returncode}")
      if not wait_for_pandad():
        raise RuntimeError("pandad did not stop; radar probe was not run")

      report_path = probe_report_path()
      status["report_path"] = str(report_path)
      status["probe_started"] = True
      probe = run_command(["timeout", "--kill-after=5s", "120s",
                           *probe_command(report_path, characterize, compare_sessions, candidate_trial)],
                          input="OK\n", stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
      status["probe_returncode"] = probe.returncode
      if probe.returncode == 124:
        status["error"] = ("radar characterization timed out; final configuration is unverified" if characterize else
                           "radar probe timed out; session restoration is unverified")

      if candidate_trial:
        recovery_path = probe_report_path()
        status['recovery_report_path'] = str(recovery_path)
        recovery = run_command(["timeout", "--kill-after=5s", "60s",
                                *probe_command(recovery_path, candidate_trial=True, restore_only=True)],
                               stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        status['recovery_returncode'] = recovery.returncode
        if recovery_path.is_file():
          restored = json.loads(recovery_path.read_text(encoding='utf-8'))
          status['restore_status'] = restored.get('restore_status')
          status['final_config_verified'] = restored.get('final_config_verified')
          status['default_session_restored'] = restored.get('default_session_restored')
        if recovery.returncode or status['restore_status'] != 'confirmed':
          status['error'] = 'independent original-configuration verification failed; do not drive until reviewed'

      if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not candidate_trial:
          status["restore_status"] = report.get("restore_status")
          status["final_config_verified"] = report.get("final_config_verified")
        status["report_status"] = report.get("status")
        if report.get("blocked_by"):
          status["blocked_by"] = report["blocked_by"]
        status["dtc_changed"] = report.get("dtc_changed")
        status["session_comparison"] = report.get("session_comparison")
        status["comparison_complete"] = report.get("comparison_complete")
        status["summary"] = summarize_report(report)
        if report.get("can_observations"):
          status["summary"]["can_observations"] = report["can_observations"]
      else:
        if candidate_trial:
          status['trial_report_missing'] = True
        status["error"] = status["error"] or "probe report missing; diagnostic result is unverified"
    except (OSError, ValueError, RuntimeError) as error:
      status["error"] = str(error)
    finally:
      if stop_attempted:
        restart = run_command(["systemctl", "start", "comma"], stdout=log, stderr=subprocess.STDOUT)
        if restart.returncode == 0:
          time.sleep(3)
          status["comma_restarted"] = run_command(["systemctl", "is-active", "--quiet", "comma"],
                                                  stdout=log, stderr=subprocess.STDOUT).returncode == 0
      status["finished_at"] = time.time()
      STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
      log.write(f"\nRESULT {json.dumps(status, ensure_ascii=False)}\n")
  spool = Path(status["report_path"]).parent if status["report_path"] else RESULT_DIR / "spool"
  spool.mkdir(parents=True, exist_ok=True)
  upload_path = spool / f"k7-security-probe-status-{uuid.uuid4()}.json"
  log_tail = LOG_FILE.read_bytes()[-MAX_UPLOADED_LOG_BYTES:].decode("utf-8", errors="replace")
  upload_path.write_text(json.dumps({"schema": "c4-k7-parked-probe-status-v1", "created_at": time.time(),
                                     "worker_status": status, "log_tail": log_tail}, ensure_ascii=False) + "\n",
                         encoding="utf-8")
  os.chmod(upload_path, 0o644)
  if not status["comma_restarted"] or status["error"] or not status["final_config_verified"]:
    return 3
  expected_restore = "not_needed" if characterize and not compare_sessions else "confirmed"
  if status["restore_status"] != expected_restore or status.get("dtc_changed"):
    return 3
  return status["probe_returncode"] if status["probe_returncode"] in (0, 2, 3, 4, 5) else 3


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
  parser.add_argument('--characterize', action='store_true', help='collect bounded identity/configuration/DTC reads without session or security requests')
  parser.add_argument('--compare-sessions', action='store_true', help='with --characterize, compare default and extended-session reads')
  parser.add_argument('--candidate-trial', action='store_true', help='stationary K7 single-candidate trial with independent restore verification')
  parser.add_argument('--from-recovery', action='store_true', help=argparse.SUPPRESS)
  parser.add_argument('--verify-parked', action='store_true', help=argparse.SUPPRESS)
  parser.add_argument('--summarize-last', action='store_true', help='summarize and queue the saved report without contacting the ECU')
  args = parser.parse_args()
  if args.summarize_last:
    if any((args.worker, args.characterize, args.compare_sessions, args.from_recovery, args.verify_parked, args.candidate_trial)):
      parser.error('--summarize-last must be used alone')
    return summarize_last()
  if args.compare_sessions and not args.characterize:
    parser.error('--compare-sessions requires --characterize')
  if args.candidate_trial and (args.characterize or args.compare_sessions):
    parser.error('--candidate-trial cannot be combined with characterization')
  if args.worker:
    if os.geteuid() != 0:
      parser.error("worker must run as root in its own systemd unit")
    return run_worker(args.characterize, args.verify_parked, args.compare_sessions, args.candidate_trial)
  return start_from_web(args.characterize, args.from_recovery, args.compare_sessions, args.candidate_trial)


if __name__ == "__main__":
  sys.exit(main())
