# 엔진을 끈 K7에서 CAN 주소와 데이터 변화만 수동 관찰한다.
"""Passively inventory CAN while the vehicle is stationary in P with ignition on."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import time
import uuid


DURATION_S = 30


def parked_state(sm):
  services = {name: {"alive": bool(sm.alive[name]), "valid": bool(sm.valid[name])}
              for name in ("carState", "deviceState")}
  reasons = [f"{name} unavailable or invalid" for name, state in services.items()
             if not state["alive"] or not state["valid"]]
  state = {"services": services, "reasons": reasons}
  if reasons:
    return state
  car = sm["carState"]
  state.update(gear=str(car.gearShifter).lower().split(".")[-1],
               v_ego=float(car.vEgo), engine_rpm=float(car.engineRpm),
               started=bool(sm["deviceState"].started))
  if state["gear"] != "park":
    reasons.append("gear is not P")
  if abs(state["v_ego"]) >= 0.1:
    reasons.append("vehicle is not stationary")
  if state["engine_rpm"] >= 1:
    reasons.append("engine RPM is not zero")
  if not state["started"]:
    reasons.append("ignition/onroad state is not ready")
  return state


def add_frame(summary, address, source, data):
  summary["bus_counts"][str(source)] += 1
  if source != 1 and not (source in (0, 2) and 0x500 <= address <= 0x53F):
    return
  key = f"{source}:0x{address:03x}:{len(data)}"
  entry = summary["addresses"].get(key)
  if entry is None:
    entry = {"count": 0, "first_hex": data.hex(), "last_hex": "",
             "and": bytearray(data), "or": bytearray(data)}
    summary["addresses"][key] = entry
  entry["count"] += 1
  entry["last_hex"] = data.hex()
  for index, value in enumerate(data):
    entry["and"][index] &= value
    entry["or"][index] |= value


def finish_summary(summary):
  return {"bus_counts": dict(summary["bus_counts"]),
          "addresses": {key: {"count": value["count"], "first_hex": value["first_hex"],
                              "last_hex": value["last_hex"],
                              "varying_mask_hex": bytes(a ^ b for a, b in zip(value["and"], value["or"])).hex()}
                        for key, value in sorted(summary["addresses"].items())}}


def observe(duration_s=DURATION_S):
  from openpilot.cereal import messaging

  sm = messaging.SubMaster(["carState", "deviceState"], poll="carState")
  state = None
  for _ in range(10):
    sm.update(1000)
    state = parked_state(sm)
    if not state["reasons"]:
      break
  report = {"schema": "c4-k7-passive-can-v1", "mode": "parked_passive_can",
            "created_at": time.time(), "duration_requested_s": duration_s,
            "transmit_performed": False, "preflight": state}
  if state["reasons"]:
    report["status"] = "preflight_blocked"
    return report

  sock = messaging.sub_sock("can", conflate=False)
  summary = {"bus_counts": Counter(), "addresses": {}}
  start = time.monotonic()
  deadline = start + duration_s
  report["status"] = "completed"
  while time.monotonic() < deadline:
    sm.update(0)
    state = parked_state(sm)
    if state["reasons"]:
      report["status"] = "stopped_vehicle_state_changed"
      report["final_state"] = state
      break
    event = messaging.recv_one_or_none(sock)
    if event is None:
      time.sleep(0.01)
      continue
    for frame in event.can:
      add_frame(summary, frame.address, frame.src, bytes(frame.dat))
  report["duration_observed_s"] = round(time.monotonic() - start, 3)
  report["can_summary"] = finish_summary(summary)
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--duration", type=int, default=DURATION_S, choices=range(1, 61), metavar="1..60")
  args = parser.parse_args()
  from tools.c4_diagnostics.parked_probe import probe_report_path

  report = observe(args.duration)
  path = probe_report_path().parent / f"k7-security-probe-passive-can-{uuid.uuid4()}.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
  os.chmod(path, 0o644)
  print(f"{report['status']}: {path}")
  print(f"CAN frames by bus: {report.get('can_summary', {}).get('bus_counts', {})}")
  return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
  raise SystemExit(main())
