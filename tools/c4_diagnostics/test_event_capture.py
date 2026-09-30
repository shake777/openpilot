import json
import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.event_capture import EventDetector, TimeRing, write_event_companion

S = 1_000_000_000


class EventCaptureTests(unittest.TestCase):
  def test_ring_keeps_only_the_last_window(self):
    ring = TimeRing(seconds=15.0)
    for second in range(30):
      ring.push(second * S, second)
    self.assertEqual(ring.drain(), list(range(14, 30)))
    self.assertEqual(ring.drain(), [])

  def test_driver_brake_during_long_control_is_an_edge(self):
    detector = EventDetector()
    self.assertIsNone(detector.update(0, 10.0, False, True, 0.0, True))
    self.assertEqual(detector.update(1 * S, 10.0, True, True, 0.0, True), "driver_brake")
    self.assertIsNone(detector.update(2 * S, 10.0, True, True, 0.0, True))

  def test_brake_without_long_control_is_ignored(self):
    detector = EventDetector()
    self.assertIsNone(detector.update(0, 10.0, True, False, 0.0, True))

  def test_hard_brake_edge(self):
    detector = EventDetector()
    self.assertIsNone(detector.update(0, 15.0, False, True, -1.9, True))
    self.assertEqual(detector.update(1 * S, 15.0, False, True, -2.0, True), "hard_brake")
    self.assertIsNone(detector.update(2 * S, 15.0, False, True, -2.5, True))

  def test_stop_behind_lead_after_driving_fast(self):
    detector = EventDetector()
    detector.update(0, 8.0, False, True, -0.5, True)
    self.assertEqual(detector.update(10 * S, 0.1, False, True, -0.5, True), "stop_behind_lead")
    # Still stopped: latched, no repeat.
    self.assertIsNone(detector.update(140 * S, 0.0, False, True, -0.5, True))

  def test_crawl_stop_does_not_trigger(self):
    detector = EventDetector()
    detector.update(0, 3.0, False, True, -0.3, True)
    self.assertIsNone(detector.update(5 * S, 0.1, False, True, -0.3, True))

  def test_cooldown_and_hourly_cap(self):
    detector = EventDetector()
    detector.update(0, 10.0, False, True, 0.0, True)
    self.assertEqual(detector.update(1 * S, 10.0, True, True, 0.0, True), "driver_brake")
    detector.update(2 * S, 10.0, False, True, 0.0, True)
    self.assertIsNone(detector.update(60 * S, 10.0, True, True, 0.0, True))  # within 120 s
    events = 0
    for minute in range(3, 120, 3):
      t = minute * 60 * S
      detector.update(t, 10.0, False, True, 0.0, True)
      events += detector.update(t + S, 10.0, True, True, 0.0, True) is not None
    self.assertLessEqual(events, 20)  # 10 per rolling hour over ~2 hours

  def test_event_companion(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      capture = Path(temp_dir) / "capture.c4radar"
      path = write_event_companion(capture, "hard_brake", 123)
      data = json.loads(path.read_text(encoding="utf-8"))
      self.assertEqual(path.name, "capture.c4event.json")
      self.assertEqual((data["schema"], data["reason"], data["label"], data["trigger_mono_ns"]),
                       ("c4-event-v1", "hard_brake", "급제동", 123))


if __name__ == "__main__":
  unittest.main()
