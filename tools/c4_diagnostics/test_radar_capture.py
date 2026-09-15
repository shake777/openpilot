# C4 레이더 캡처 형식과 기간 제한 없는 자동 전송 상태를 검증하는 테스트
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.c4_diagnostics.auto_upload import deterministic_upload_id, load_or_start_state, pending_captures, upload_one
from tools.c4_diagnostics.radar_capture import MAX_CAPTURE_BYTES, RadarCaptureWriter, iter_records
from tools.c4_diagnostics.scene_capture import MAX_SCENE_BYTES, SceneCaptureWriter, build_scene_frame
from tools.c4_diagnostics.upload import MAX_TOTAL_BYTES, UploadConfig


class IntegerOnlyList:
  def __init__(self, values):
    self.values = values

  def __len__(self):
    return len(self.values)

  def __getitem__(self, index):
    if not isinstance(index, int):
      raise TypeError("an integer is required")
    return self.values[index]


class TestRadarCapture(unittest.TestCase):
  def test_capture_keeps_only_radar_addresses_and_original_bytes(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = RadarCaptureWriter(Path(temp_dir), wall_time=1000)
      self.assertTrue(writer.append(123, 0x500, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"))
      self.assertTrue(writer.append(124, 0x420, 0, b"scc-data"))
      self.assertFalse(writer.append(125, 0x123, 0, b"ignored"))
      capture = writer.finalize()
      self.assertIsNotNone(capture)
      self.assertEqual(list(iter_records(capture)), [
        (123, 0x500, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"),
        (124, 0x420, 0, b"scc-data"),
      ])

  def test_empty_capture_is_removed(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = RadarCaptureWriter(Path(temp_dir), wall_time=1000)
      self.assertIsNone(writer.finalize())
      self.assertEqual(list(Path(temp_dir).iterdir()), [])

  def test_state_has_no_expiration(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      state_path = Path(temp_dir) / "state.json"
      state = load_or_start_state(state_path, now=1000)
      self.assertNotIn("expires_at", state)
      self.assertEqual(load_or_start_state(state_path, now=2000), state)

  def test_expiring_state_is_migrated_without_losing_upload_history(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      state_path = Path(temp_dir) / "state.json"
      state_path.write_text('{"schema":1,"started_at":1000,"expires_at":2000,"uploaded":{"001.c4radar":{}}}', encoding="utf-8")
      state = load_or_start_state(state_path, now=3000)
      self.assertEqual(state, {"schema": 2, "started_at": 1000, "uploaded": {"001.c4radar": {}}})
      self.assertEqual(load_or_start_state(state_path, now=4000), state)

  def test_pending_and_upload_id_are_stable(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      spool = Path(temp_dir)
      first = spool / "001.c4radar"
      second = spool / "002.c4radar"
      first.write_bytes(b"first")
      second.write_bytes(b"second")
      second.with_suffix(".c4scene").write_text('{"schema":"c4-scene-v1"}\n', encoding="utf-8")
      state = {"uploaded": {first.name: {}}}
      self.assertEqual(pending_captures(spool, state), [second])
      self.assertEqual(
        deterministic_upload_id("c4-001", second, "abc"),
        deterministic_upload_id("c4-001", second, "abc"),
      )

  def test_pending_ignores_incomplete_capture_without_scene(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      capture = Path(temp_dir) / "crash-fragment.c4radar"
      capture.write_bytes(b"partial")
      self.assertEqual(pending_captures(Path(temp_dir), {"uploaded": {}}), [])

  def test_scene_capture_keeps_radar_model_and_control_fields(self):
    xy = SimpleNamespace(x=IntegerOnlyList([0.0, 10.0]), y=IntegerOnlyList([0.0, 0.5]))
    radar_point = SimpleNamespace(trackId=7, dRel=20.0, yRel=-1.2, vRel=-2.0, vLead=8.0, aRel=0.1,
                                  measured=True, radarSource="frontRadar", trackState=2)
    lead = SimpleNamespace(status=True, radar=True, radarTrackId=7, dRel=20.0, yRel=-1.2, vRel=-2.0,
                           vLead=8.0, dPath=0.2, modelProb=0.8)
    model_lead = SimpleNamespace(prob=0.8, x=IntegerOnlyList([21.5]), y=IntegerOnlyList([1.2]),
                                 v=IntegerOnlyList([8.0]), a=IntegerOnlyList([-0.1]))
    frame = build_scene_frame(
      123,
      SimpleNamespace(vEgo=10.0, steeringAngleDeg=2.0),
      SimpleNamespace(position=xy, laneLines=IntegerOnlyList([xy] * 4),
                      laneLineProbs=IntegerOnlyList([0.9] * 4), leadsV3=IntegerOnlyList([model_lead])),
      SimpleNamespace(points=IntegerOnlyList([radar_point])),
      SimpleNamespace(leadOne=lead, leadTwo=lead),
      SimpleNamespace(longActive=True, enabled=True, actuators=SimpleNamespace(accel=-0.5)),
    )
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = SceneCaptureWriter(Path(temp_dir), "capture", 1000)
      self.assertTrue(writer.append(frame))
      output = writer.finalize()
      lines = output.read_text(encoding="utf-8").splitlines()
    self.assertEqual(__import__("json").loads(lines[0]), {"schema": "c4-scene-v1"})
    saved = __import__("json").loads(lines[1])
    self.assertEqual(saved["points"][0]["track_id"], 7)
    self.assertEqual(saved["lead_one"]["d_rel"], 20.0)
    self.assertEqual(saved["target_accel"], -0.5)

  def test_upload_includes_scene_companion(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      root = Path(temp_dir)
      capture = root / "capture.c4radar"
      scene = root / "capture.c4scene"
      capture.write_bytes(b"radar")
      scene.write_text('{"schema":"c4-scene-v1"}\n', encoding="utf-8")
      state_path = root / "state.json"
      state = {"schema": 2, "started_at": 1, "uploaded": {}}
      with patch("tools.c4_diagnostics.auto_upload.upload", return_value={"upload_id": "id"}) as send:
        upload_one(UploadConfig("https://example.com", "key"), "c4-001", state, state_path, capture)
      self.assertEqual(send.call_args.args[3], [capture, scene])

  def test_upload_ignores_empty_optional_companion(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      root = Path(temp_dir)
      capture = root / "capture.c4radar"
      companion = root / "capture.meminfo"
      capture.write_bytes(b"radar")
      companion.touch()
      state_path = root / "state.json"
      state = {"schema": 2, "started_at": 1, "uploaded": {}}
      with patch("tools.c4_diagnostics.auto_upload.upload", return_value={"upload_id": "id"}) as send:
        upload_one(UploadConfig("https://example.com", "key"), "c4-001", state, state_path, capture)
      self.assertEqual(send.call_args.args[3], [capture])

  def test_capture_group_stays_below_upload_limit(self):
    self.assertLess(MAX_CAPTURE_BYTES + MAX_SCENE_BYTES, MAX_TOTAL_BYTES)


if __name__ == "__main__":
  unittest.main()
