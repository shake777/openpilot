#!/usr/bin/env python3
"""Fake Carrot Navi v2 sender (stands in for patched TMAP/Naver) for bench-testing the AA Probe bridge.

Connects like the app: control socket -> requirements_query (full 28-item catalog) -> manifest ->
manifest_applied -> one socket per enabled guidance JSON item, then sends a scripted route of
guidance_current/guidance_next/navigation_status/route updates. Requires aiohttp.

usage: python3 fake_navi_sender.py --host 127.0.0.1 --port 7714 [--seconds 30]
"""
import argparse
import asyncio
import time

import aiohttp

JSON_NAMES = (
  "vehicle", "guidance_current", "guidance_next", "lane_current", "lane_ahead", "speed",
  "traffic_signal", "crossroad", "route", "navigation_status", "app_status", "camera_state",
  "composition_state",
)
IMAGE_NAMES = (
  "tbt_current_compact", "tbt_current_full", "tbt_next", "traffic_signal", "lane_top", "lane_bottom",
  "safety_primary", "safety_secondary", "safety_section", "crossroad_minimized", "crossroad_expanded",
  "center_tbt_icon", "center_tbt_text", "center_tbt_fee",
)
CATALOG = [("json", n) for n in JSON_NAMES] + [("image", n) for n in IMAGE_NAMES] + [("render", "map_main")]
SENT = ("guidance_current", "guidance_next", "navigation_status", "route")
# (turn_type, road) — TMAP codes: right, left, U-turn, keep-left, rotary(right exit), destination
ROUTE = [(13, "테스트1로"), (12, "테스트2로"), (14, "테스트3로"), (17, "테스트4로"), (133, "테스트5로"), (201, "목적지")]


def now_ms() -> int:
  return int(time.time() * 1000)


def envelope(session: str, revision: int, stream: dict, seq: int, value) -> dict:
  return {
    "type": "item_update", "protocol_version": 2, "session_id": session, "manifest_revision": revision,
    "schema_version": 1, "kind": "json", "name": stream["name"], "stream_handle": stream["stream_handle"],
    "sequence": seq, "source_timestamp_ms": now_ms(), "sent_at_ms": now_ms(),
    "present": value is not None, "value": value, **({} if value is not None else {"reason": "source_absent"}),
  }


async def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("--host", default="127.0.0.1")
  ap.add_argument("--port", type=int, default=7714)
  ap.add_argument("--seconds", type=float, default=30.0)
  args = ap.parse_args()
  base = f"ws://{args.host}:{args.port}/api/navi/ws/v2"
  async with aiohttp.ClientSession() as http:
    control = await http.ws_connect(f"{base}/control/fake-1.0")
    await control.send_json({
      "type": "requirements_query", "protocol_version": 2, "timestamp_ms": now_ms(), "app_version": "fake-1.0",
      "catalog_revision": 1, "limits": {"max_binary_frame_bytes": 8388608, "max_total_bitrate_kbps": 12000},
      "streams": [{"kind": k, "name": n, "schema_version": 1, "nullable": True} for k, n in CATALOG],
    })
    manifest = await control.receive_json(timeout=5)
    assert manifest["type"] == "subscription_manifest", manifest
    session, revision = manifest["session_id"], manifest["revision"]
    enabled = {s["name"]: s for s in manifest["streams"] if s["kind"] == "json" and s["enabled"]}
    print("manifest", session, "enabled json:", sorted(enabled))
    await control.send_json({"type": "manifest_applied", "protocol_version": 2, "timestamp_ms": now_ms(),
                             "session_id": session, "revision": revision, "effective_config": manifest["streams"]})
    sockets = {}
    for name in SENT:
      if name in enabled:
        sockets[name] = await http.ws_connect(f"{base}/json/{session}/{name}")
    seq = {name: 0 for name in sockets}

    async def send(name, value):
      if name not in sockets:
        return
      seq[name] += 1
      await sockets[name].send_json(envelope(session, revision, enabled[name], seq[name], value))

    await send("navigation_status", {"mode": "guiding", "guidance_active": True, "off_route": False, "route_present": True})
    start = time.monotonic()
    leg_m = 300
    total = leg_m * len(ROUTE)
    while time.monotonic() - start < args.seconds:
      travelled = int((time.monotonic() - start) * 60)  # 60 m/s so the script walks the whole route
      idx = min(travelled // leg_m, len(ROUTE) - 1)
      dist = max(0, leg_m - travelled % leg_m) if idx < len(ROUTE) - 1 else max(0, total - travelled)
      turn, road = ROUTE[idx]
      await send("guidance_current", {"distance_m": dist, "time_sec": dist // 15, "turn_type": turn,
                                      "road_name": road, "main_text": "", "near_direction": f"{road} 방면"})
      if idx + 1 < len(ROUTE):
        nturn, nroad = ROUTE[idx + 1]
        await send("guidance_next", {"distance_m": dist + leg_m, "time_sec": (dist + leg_m) // 15,
                                     "turn_type": nturn, "road_name": nroad})
      else:
        await send("guidance_next", None)
      await send("route", {"remain_distance_m": max(0, total - travelled), "remain_time_sec": max(0, total - travelled) // 15})
      await asyncio.sleep(0.25)
    await send("guidance_current", None)
    await send("navigation_status", {"mode": "idle", "guidance_active": False, "off_route": False, "route_present": False})
    await asyncio.sleep(1)
    for ws in sockets.values():
      await ws.close()
    await control.close()
    print("done")


if __name__ == "__main__":
  asyncio.run(main())
