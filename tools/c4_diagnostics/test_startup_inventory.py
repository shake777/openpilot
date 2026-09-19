# 자동 레이더 진단의 실행 제한과 읽기 전용 송신 및 독립 업로드를 검증하는 테스트
import ast
import json
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.c4_diagnostics import startup_inventory as inventory
from tools.c4_diagnostics.auto_upload import pending_captures, upload_one
from tools.c4_diagnostics.upload import UploadConfig, UploadError


Frame = namedtuple("Frame", "address dat src")


def parked_state():
  return SimpleNamespace(canValid=True, canTimeout=False, vEgo=0.0, vEgoRaw=0.0, standstill=True,
                         wheelSpeeds=SimpleNamespace(fl=0.0, fr=0.0, rl=0.0, rr=0.0), gearShifter="park",
                         cruiseState=SimpleNamespace(enabled=False), gasPressed=False)


def inactive_panda():
  return SimpleNamespace(safetyModel="elm327", controlsAllowed=False, harnessStatus="normal",
                         ignitionLine=True, ignitionCan=False)


class QueryFactory:
  def __init__(self, replies=None, frames=(), outgoing=None):
    self.requests = []
    self.replies = replies if replies is not None else {
      inventory.REQUESTS[0]: b"K7 SCC firmware",
      inventory.REQUESTS[1]: b"\x00" * 6,
      inventory.REQUESTS[2]: b"K7 SCC hardware",
      inventory.REQUESTS[3]: b"K7 SCC software",
    }
    self.frames = list(frames)
    self.outgoing = outgoing

  def __call__(self, send, receive, bus, addresses, requests, responses, **kwargs):
    request = requests[0]
    self.requests.append(request)
    assert bus == 0 and addresses == [0x7d0] and responses == [b"\x62" + request[1:]]
    assert kwargs["response_pending_timeout"] == inventory.QUERY_SECONDS

    def get_data(timeout, total_timeout):
      assert timeout == total_timeout == inventory.QUERY_SECONDS
      send([self.outgoing or Frame(0x7d0, (b"\x03" + request).ljust(8, b"\x00"), 0)])
      receive(True)
      return {(0x7d0, None): self.replies[request]} if request in self.replies else {}

    return SimpleNamespace(get_data=get_data)


class TestStartupInventory(unittest.TestCase):
  def check_gate(self, cs=None, panda=None, **overrides):
    kwargs = dict(can_age=0, panda_age=0, panda_valid=True, controls_ready=False)
    kwargs.update(overrides)
    return inventory.stationary_reason(cs or parked_state(), [panda or inactive_panda()], **kwargs)

  def test_stationary_gate(self):
    self.assertIsNone(self.check_gate())
    for field, value in (("vEgo", 0.1), ("vEgoRaw", float("nan")), ("standstill", False),
                         ("canValid", False), ("canTimeout", True), ("gearShifter", "drive"),
                         ("gasPressed", True)):
      with self.subTest(field=field):
        cs = parked_state()
        setattr(cs, field, value)
        self.assertIsNotNone(self.check_gate(cs))
    cs = parked_state()
    cs.wheelSpeeds.fl = 0.1
    self.assertIsNotNone(self.check_gate(cs))
    cs = parked_state()
    cs.cruiseState.enabled = True
    self.assertIsNotNone(self.check_gate(cs))

  def test_unknown_stale_or_active_conditions_block(self):
    for kwargs in (dict(can_age=0.2), dict(panda_age=1.1), dict(panda_valid=False), dict(controls_ready=True)):
      self.assertIsNotNone(self.check_gate(**kwargs))
    for field, value in (("safetyModel", "hyundaiLegacy"), ("controlsAllowed", True),
                         ("harnessStatus", "notConnected"), ("ignitionLine", False)):
      panda = inactive_panda()
      setattr(panda, field, value)
      self.assertIsNotNone(self.check_gate(panda=panda))
    self.assertIsNotNone(inventory.stationary_reason(None, [], can_age=0, panda_age=0, panda_valid=True, controls_ready=False))
    self.assertEqual(inventory.stationary_reason(parked_state(), [inactive_panda()] * 2,
                     can_age=0, panda_age=0, panda_valid=True, controls_ready=False), "multiple_pandas_not_supported")

  def collect(self, factory=None, ready=lambda: None):
    factory = factory or QueryFactory()
    sent, report = [], {}
    inventory.collect_inventory(lambda wait=False: [factory.frames], lambda frames: sent.extend(frames), ready, factory, report)
    return report, sent, factory

  def test_positive_results_and_only_fixed_requests(self):
    report, sent, factory = self.collect()
    self.assertEqual(report["status"], "complete")
    self.assertEqual(factory.requests, list(inventory.REQUESTS))
    self.assertEqual(report["f100"]["ascii"], "K7 SCC firmware")
    self.assertEqual(report["did_0142"]["raw_hex"], "00" * 6)
    self.assertEqual(report["f187"]["ascii"], "K7 SCC hardware")
    self.assertEqual(report["f195"]["ascii"], "K7 SCC software")
    self.assertEqual([f.dat[:4] for f in sent], [b"\x03\x22\xf1\x00", b"\x03\x22\x01\x42",
                                                   b"\x03\x22\xf1\x87", b"\x03\x22\xf1\x95"])

  def test_no_response_stops_before_second_did(self):
    report, sent, factory = self.collect(QueryFactory(replies={}))
    self.assertEqual(report["status"], "no_positive_response")
    self.assertEqual(report["did_0142"]["status"], "not_read")
    self.assertEqual(len(factory.requests), 1)

  def test_rejection_and_tx_blocked_are_distinct(self):
    for frames, status in (([Frame(0x7d8, b"\x03\x7f\x22\x31", 0)], "rejected"),
                           ([Frame(0x7d0, b"\x03\x22\xf1\x00", 192)], "tx_blocked")):
      report, _, _ = self.collect(QueryFactory(replies={}, frames=frames))
      self.assertEqual(report["f100"]["status"], status)
      if status == "rejected":
        self.assertEqual(report["f100"]["nrc"], "0x31")

  def test_wrong_bus_and_pending_are_not_final_rejections(self):
    for frame in (Frame(0x7d8, b"\x03\x7f\x22\x31", 2), Frame(0x7d8, b"\x03\x7f\x22\x78", 0)):
      report, _, _ = self.collect(QueryFactory(replies={}, frames=[frame]))
      self.assertEqual(report["f100"]["status"], "no_positive_response")

  def test_f100_preserved_when_second_read_fails(self):
    report, _, _ = self.collect(QueryFactory(replies={inventory.REQUESTS[0]: b"firmware"}))
    self.assertEqual(report["status"], "partial")
    self.assertEqual(report["f100"]["raw_hex"], b"firmware".hex())

  def test_optional_identification_rejection_does_not_stop_remaining_reads(self):
    replies = {
      inventory.REQUESTS[0]: b"firmware",
      inventory.REQUESTS[1]: b"\x00" * 5,
      inventory.REQUESTS[3]: b"software",
    }
    report, _, factory = self.collect(QueryFactory(replies=replies))
    self.assertEqual(factory.requests, list(inventory.REQUESTS))
    self.assertEqual(report["f187"]["status"], "no_positive_response")
    self.assertEqual(report["f195"]["ascii"], "software")
    self.assertEqual(report["status"], "complete")

  def test_movement_or_state_loss_stops_transmission(self):
    for reason in ("vehicle_not_stationary", "panda_state_unavailable", "controls_already_ready"):
      report, sent, _ = self.collect(ready=lambda: reason)
      self.assertEqual(sent, [])
      self.assertEqual(report["status"], "aborted")
    calls = iter([None, None, "vehicle_not_stationary"])
    report, sent, factory = self.collect(ready=lambda: next(calls))
    self.assertEqual(len(sent), 1)
    self.assertEqual(factory.requests, [inventory.REQUESTS[0]])
    self.assertEqual(report["status"], "aborted")

  def test_non_read_commands_and_wrong_addresses_blocked(self):
    for frame in (Frame(0x7d0, b"\x02\x10\x07".ljust(8, b"\x00"), 0),
                  Frame(0x7d0, b"\x03\x2e\x01\x42".ljust(8, b"\x00"), 0),
                  Frame(0x7d1, b"\x03\x22\xf1\x00".ljust(8, b"\x00"), 0),
                  Frame(0x7d0, b"\x03\x22\xf1\x00".ljust(8, b"\x00"), 2)):
      report, sent, _ = self.collect(QueryFactory(outgoing=frame))
      self.assertEqual(sent, [])
      self.assertEqual(report["status"], "aborted")

  def test_isotp_receive_flow_control_and_raw_limit(self):
    self.assertTrue(inventory.allowed_read_frame(Frame(0x7d0, b"\x30\x00\x0a".ljust(8, b"\x00"), 0), inventory.REQUESTS[0]))
    report, _, _ = self.collect(QueryFactory(frames=[Frame(0x7d8, b"\x03\x62\x01\x42", 0)] * 100))
    self.assertEqual(len(report["raw_frames"]), inventory.MAX_RAW_FRAMES)

  def test_exception_is_recorded(self):
    def broken_query(*args, **kwargs):
      raise RuntimeError("do not expose exception payloads")
    report = {}
    inventory.collect_inventory(lambda wait=False: [], lambda frames: None, lambda: None, broken_query, report)
    self.assertEqual(report["f100"], {"status": "error", "error_type": "RuntimeError"})

  def startup(self, root, cs=None, frames=True):
    clock = [0.0]
    cs = cs or parked_state()
    factory = QueryFactory()
    ci = SimpleNamespace(CP=SimpleNamespace(carFingerprint="KIA_K7"),
                         update=lambda packets: cs() if callable(cs) else cs)
    class Sm:
      valid = {"pandaStates": True}
      updated = {"pandaStates": True}
      def update(self, timeout): pass
      def __getitem__(self, key): return [inactive_panda()]
    def receive(wait=False):
      clock[0] += 0.1
      return [[Frame(0x386, b"\x00" * 8, 0)]] if frames else []
    params = SimpleNamespace(get_bool=lambda key: False, get_int=lambda key: 0)
    config = UploadConfig("https://example.com", "not-a-real-key", spool_dir=str(root))
    sent = []
    with patch.object(inventory, "load_config", return_value=config), \
         patch.object(inventory.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(Path, "read_text", return_value="01234567-1234-1234-1234-123456789abc"), \
         patch.dict("sys.modules", {"opendbc.car.isotp_parallel_query": SimpleNamespace(IsoTpParallelQuery=factory)}):
      inventory.run_startup_inventory(ci, Sm(), params, receive, lambda frames: sent.extend(frames))
    return factory, sent

  def test_startup_persists_and_only_attempts_once_per_boot(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      factory, sent = self.startup(root)
      self.assertEqual(len(sent), 4)
      report = json.loads(next(root.glob("*.json")).read_text())
      self.assertEqual(report["status"], "complete")
      self.assertFalse(report["write_performed"])
      self.assertFalse(report["session_changed"])
      self.assertFalse(report["safety_mode_changed"])
      factory, sent = self.startup(root)
      self.assertEqual(sent, [])
      self.assertEqual(factory.requests, [])

  def test_startup_skips_moving_or_no_data_and_reports_reason(self):
    for moving in (True, False):
      with tempfile.TemporaryDirectory() as tmp:
        cs = parked_state()
        if moving:
          cs.vEgo = 1.0
        factory, sent = self.startup(Path(tmp), cs, frames=moving)
        self.assertEqual(sent, [])
        report = json.loads(next(Path(tmp).glob("*.json")).read_text())
        self.assertEqual(report["status"], "skipped")
        self.assertIn(report["reason"], ("vehicle_not_stationary", "fresh_vehicle_state_unavailable"))
        if moving:
          self.assertEqual(report["stationary_evidence"]["v_ego"], 1.0)
          self.assertEqual(report["stationary_evidence"]["gear"], "park")
        else:
          self.assertNotIn("stationary_evidence", report)

  def test_startup_reports_gear_and_nonfinite_speed_without_transmission(self):
    for gear, speed, reason, expected_speed in (("drive", 0.0, "gear_not_park", 0.0),
                                                ("park", float("nan"), "vehicle_not_stationary", None)):
      with tempfile.TemporaryDirectory() as tmp:
        cs = parked_state()
        cs.gearShifter = gear
        cs.vEgo = speed
        factory, sent = self.startup(Path(tmp), cs)
        self.assertEqual(sent, [])
        self.assertEqual(factory.requests, [])
        report = json.loads(next(Path(tmp).glob("*.json")).read_text(), parse_constant=lambda value: self.fail(value))
        self.assertEqual(report["reason"], reason)
        self.assertEqual(report["stationary_evidence"]["gear"], gear)
        self.assertEqual(report["stationary_evidence"]["v_ego"], expected_speed)

  def test_startup_waits_for_transient_gear_state(self):
    with tempfile.TemporaryDirectory() as tmp:
      updates = 0
      def changing_state():
        nonlocal updates
        updates += 1
        cs = parked_state()
        if updates < 3:
          cs.gearShifter = "unknown"
        return cs
      factory, sent = self.startup(Path(tmp), changing_state)
      self.assertEqual(factory.requests, list(inventory.REQUESTS))
      self.assertEqual(len(sent), 4)
      report = json.loads(next(Path(tmp).glob("*.json")).read_text())
      self.assertEqual(report["status"], "complete")

  def test_prior_interrupted_attempt_is_not_retried(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / "radar-inventory-01234567-1234-1234-1234-123456789abc.claim").touch()
      _, sent = self.startup(root)
      self.assertEqual(sent, [])
      self.assertEqual(json.loads(next(root.glob("*.json")).read_text())["status"], "interrupted")

  def test_reports_upload_without_scene_and_use_existing_retry_state(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      report = root / "radar-inventory-test.json"
      inventory.save_report(report, {"status": "skipped"})
      (root / "radar-inventory-incomplete.tmp").touch()
      state = {"schema": 2, "uploaded": {}}
      self.assertEqual(pending_captures(root, state), [report])
      config = UploadConfig("https://example.com", "key")
      with patch("tools.c4_diagnostics.auto_upload.upload", side_effect=UploadError("offline")):
        with self.assertRaises(UploadError):
          upload_one(config, "c4-test", state, root / "state.json", report)
      self.assertEqual(state["uploaded"], {})
      with patch("tools.c4_diagnostics.auto_upload.upload", return_value={"upload_id": "ok"}) as send:
        upload_one(config, "c4-test", state, root / "state.json", report)
      self.assertEqual(send.call_args.args[3], [report])
      self.assertEqual(pending_captures(root, state), [])
      self.assertTrue(report.is_file())

  def test_hook_precedes_firmware_query_done(self):
    source = Path(__file__).resolve().parents[2] / "openpilot/selfdrive/car/card.py"
    text = source.read_text(encoding="utf-8")
    ast.parse(text)
    self.assertLess(text.index("run_startup_inventory(self.CI"), text.index('self.params.put_bool("FirmwareQueryDone", True)'))


if __name__ == "__main__":
  unittest.main()
