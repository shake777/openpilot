# 6999 터미널에서 K7 레이더 무쓰기 진단을 별도 일회성 서비스로 실행한다.
"""Run the existing parked K7 probe outside the comma service cgroup."""

import argparse
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
RESULT_DIR = Path("/data/c4-diagnostics")
STATUS_FILE = RESULT_DIR / "k7-parked-probe-status.json"
LOG_FILE = RESULT_DIR / "k7-parked-probe.log"


def run_command(command, **kwargs):
  return subprocess.run(command, check=False, text=True, **kwargs)


def start_from_web():
  if not Path("/AGNOS").exists() or not PROBE.is_file():
    print("C4 AGNOS or radar probe script not found; nothing was started")
    return 2
  answer = input("Parked, parking brake set, engine OFF and ignition ON? Type PARKED to start: ").strip()
  if answer != "PARKED":
    print("Cancelled; nothing was started")
    return 2

  unit = "c4-k7-parked-probe"
  command = ["sudo", "-n", "systemd-run", "--collect", f"--unit={unit}",
             f"--working-directory={ROOT}", "--", sys.executable, str(Path(__file__).resolve()), "--worker"]
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


def probe_command(report_path):
  return ["runuser", "-u", "comma", "--", "/usr/bin/env",
          f"PYTHONPATH={ROOT}:/data/pythonpath", sys.executable, str(PROBE),
          "--k7-security-probe", "--probe-output", str(report_path)]


def run_worker():
  RESULT_DIR.mkdir(parents=True, exist_ok=True)
  status = {"started_at": time.time(), "probe_started": False, "probe_returncode": None,
            "report_path": None, "restore_status": None, "final_config_verified": None,
            "comma_restarted": False, "error": None}
  with LOG_FILE.open("w", encoding="utf-8") as log:
    try:
      stop = run_command(["systemctl", "stop", "comma"], stdout=log, stderr=subprocess.STDOUT)
      if stop.returncode:
        raise RuntimeError(f"systemctl stop comma failed: {stop.returncode}")
      if not wait_for_pandad():
        raise RuntimeError("pandad did not stop; radar probe was not run")

      report_path = probe_report_path()
      status["report_path"] = str(report_path)
      status["probe_started"] = True
      probe = run_command(["timeout", "--kill-after=5s", "120s", *probe_command(report_path)],
                          input="OK\n", stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
      status["probe_returncode"] = probe.returncode
      if probe.returncode == 124:
        status["error"] = "radar probe timed out; session restoration is unverified"

      if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        status["restore_status"] = report.get("restore_status")
        status["final_config_verified"] = report.get("final_config_verified")
    except (OSError, ValueError, RuntimeError) as error:
      status["error"] = str(error)
    finally:
      restart = run_command(["systemctl", "start", "comma"], stdout=log, stderr=subprocess.STDOUT)
      if restart.returncode == 0:
        time.sleep(3)
        status["comma_restarted"] = run_command(["systemctl", "is-active", "--quiet", "comma"],
                                                stdout=log, stderr=subprocess.STDOUT).returncode == 0
      status["finished_at"] = time.time()
      STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
      log.write(f"\nRESULT {json.dumps(status, ensure_ascii=False)}\n")
  return 0 if status["comma_restarted"] else 3


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
  args = parser.parse_args()
  if args.worker:
    if os.geteuid() != 0:
      parser.error("worker must run as root in its own systemd unit")
    return run_worker()
  return start_from_web()


if __name__ == "__main__":
  sys.exit(main())
