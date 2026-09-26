# K7 정차 CAN 수동 조사가 주소와 안전 차단을 올바르게 기록하는지 검증한다.
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.c4_diagnostics.auto_upload import pending_captures
from tools.c4_diagnostics.parked_can_survey import add_frame, finish_summary, parked_state


class TestParkedCanSurvey(unittest.TestCase):
  def test_all_bus_one_addresses_and_other_bus_track_candidates(self):
    from collections import Counter
    summary = {"bus_counts": Counter(), "addresses": {}}
    add_frame(summary, 0x201, 1, b"\x00\x02")
    add_frame(summary, 0x201, 1, b"\x01\x02")
    add_frame(summary, 0x500, 0, b"\x03")
    add_frame(summary, 0x201, 128, b"\xff")
    result = finish_summary(summary)
    self.assertEqual(result["bus_counts"], {"1": 2, "0": 1, "128": 1})
    self.assertEqual(result["addresses"]["1:0x201:2"],
                     {"count": 2, "first_hex": "0002", "last_hex": "0102", "varying_mask_hex": "0100"})
    self.assertIn("0:0x500:1", result["addresses"])
    self.assertNotIn("128:0x201:1", result["addresses"])

  def test_engine_off_ignition_on_park_required(self):
    car = SimpleNamespace(gearShifter="park", vEgo=0.0, engineRpm=0.0)
    sm = MagicMock()
    sm.__getitem__.side_effect = {"carState": car, "deviceState": SimpleNamespace(started=True)}.__getitem__
    sm.alive = {"carState": True, "deviceState": True}
    sm.valid = {"carState": True, "deviceState": True}
    self.assertEqual(parked_state(sm)["reasons"], [])
    car.engineRpm = 800.0
    self.assertIn("engine RPM is not zero", parked_state(sm)["reasons"])
    car.engineRpm = 0.0
    sm["deviceState"].started = False
    self.assertIn("ignition/onroad state is not ready", parked_state(sm)["reasons"])

  def test_report_name_is_queued_by_existing_uploader(self):
    with TemporaryDirectory() as directory:
      path = Path(directory) / "k7-security-probe-passive-can-test.json"
      path.write_text("{}", encoding="utf-8")
      self.assertEqual(pending_captures(Path(directory), {"uploaded": {}}), [path])
