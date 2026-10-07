# C4 진단 묶음을 사건(운전자 브레이크 개입·급제동·정지 접근) 순간에 남기기 위한 모듈
import json
import os
from collections import deque
from pathlib import Path


EVENT_SCHEMA = "c4-event-v1"
EVENT_PRE_SECONDS = 15.0
EVENT_POST_SECONDS = 5.0
EVENT_COOLDOWN_SECONDS = 120.0
EVENT_MAX_PER_HOUR = 10
HARD_BRAKE_ACCEL = -2.0
STOP_FROM_SPEED_MS = 20.0 / 3.6
STOPPED_SPEED_MS = 0.3
RESTART_SPEED_MS = 1.0

EVENT_REASONS = {
  "driver_brake": "운전자 브레이크 개입",
  "hard_brake": "급제동",
  "stop_behind_lead": "앞차 뒤 정지",
}


class TimeRing:
  """Items from the last `seconds`, oldest first, keyed by monotonic ns."""

  def __init__(self, seconds: float = EVENT_PRE_SECONDS):
    self.window_ns = int(seconds * 1e9)
    self.items: deque = deque()

  def push(self, mono_ns: int, item) -> None:
    self.items.append((mono_ns, item))
    while self.items and mono_ns - self.items[0][0] > self.window_ns:
      self.items.popleft()

  def drain(self) -> list:
    items = [item for _, item in self.items]
    self.items.clear()
    return items


class EventDetector:
  """Edge-triggered review events with a cooldown and an hourly cap."""

  def __init__(self):
    self.prev_brake = False
    self.prev_hard = False
    self.last_fast_ns = None
    self.stop_latched = False
    self.last_event_ns = None
    self.recent: deque = deque()

  def update(self, mono_ns: int, v_ego: float, brake_pressed: bool, long_active: bool,
             target_accel: float, has_lead: bool) -> str | None:
    reason = None
    if long_active and brake_pressed and not self.prev_brake:
      reason = "driver_brake"
    # At standstill the stop-hold request (often -2.0) is not braking (2026-10-07 false events).
    hard = long_active and target_accel <= HARD_BRAKE_ACCEL and v_ego >= RESTART_SPEED_MS
    if reason is None and hard and not self.prev_hard:
      reason = "hard_brake"
    if v_ego >= STOP_FROM_SPEED_MS:
      self.last_fast_ns = mono_ns
    if v_ego > RESTART_SPEED_MS:
      self.stop_latched = False
    if (reason is None and not self.stop_latched and v_ego < STOPPED_SPEED_MS and has_lead
        and self.last_fast_ns is not None and mono_ns - self.last_fast_ns <= EVENT_PRE_SECONDS * 1e9):
      reason = "stop_behind_lead"
    if v_ego < STOPPED_SPEED_MS and has_lead:
      self.stop_latched = True
    self.prev_brake = bool(brake_pressed)
    self.prev_hard = hard
    return reason if reason is not None and self._allowed(mono_ns) else None

  def _allowed(self, mono_ns: int) -> bool:
    while self.recent and mono_ns - self.recent[0] > 3600e9:
      self.recent.popleft()
    if self.last_event_ns is not None and mono_ns - self.last_event_ns < EVENT_COOLDOWN_SECONDS * 1e9:
      return False
    if len(self.recent) >= EVENT_MAX_PER_HOUR:
      return False
    self.last_event_ns = mono_ns
    self.recent.append(mono_ns)
    return True


def write_event_companion(capture: Path, reason: str, trigger_mono_ns: int) -> Path:
  target = capture.with_suffix(".c4event.json")
  target.write_text(json.dumps({"schema": EVENT_SCHEMA, "reason": reason, "label": EVENT_REASONS.get(reason, reason),
                                "trigger_mono_ns": int(trigger_mono_ns)}, ensure_ascii=False), encoding="utf-8")
  os.chmod(target, 0o600)
  return target
