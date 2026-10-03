# C4 레이더 캡처 형식과 기간 제한 없는 자동 전송 상태를 검증하는 테스트
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.c4_diagnostics.auto_upload import deterministic_upload_id, load_or_start_state, pending_captures, upload_one
from tools.c4_diagnostics.can_inventory import MAX_INVENTORY_BYTES, CanInventoryWriter
from tools.c4_diagnostics.qcamera_capture import (
  MAX_QCAMERA_BYTES,
  QCameraCaptureWriter,
  REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS,
  VIDEO_DURATION_SECONDS,
  next_representative_video_time,
  representative_video_due,
  plan_bundle_start,
)
from tools.c4_diagnostics.radar_capture import MAX_CAPTURE_BYTES, MAX_PENDING_DIAGNOSTICS, RadarCaptureWriter, capture_can_frame, iter_records
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
  def test_radar_diagnostic_frames_are_not_captured(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      pending = deque(maxlen=MAX_PENDING_DIAGNOSTICS)
      capture_can_frame(None, pending, 100, 0x7d0, 0, b'\x03\x22\xf1\x00')
      capture_can_frame(None, pending, 101, 0x7d8, 0, b'\x03\x7f\x22\x31')
      self.assertFalse(pending)
      writer = RadarCaptureWriter(Path(temp_dir), wall_time=1000)
      capture_can_frame(writer, pending, 103, 0x420, 0, b'onroad')
      capture_can_frame(writer, pending, 104, 0x7d8, 2, b'\x03\x6e\x01\x42')
      self.assertEqual(list(iter_records(writer.finalize())), [(103, 0x420, 0, b'onroad')])

  def test_capture_keeps_only_radar_addresses_and_original_bytes(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = RadarCaptureWriter(Path(temp_dir), wall_time=1000)
      self.assertTrue(writer.append(123, 0x500, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"))
      self.assertTrue(writer.append(124, 0x238, 1, b"classic!"))
      self.assertTrue(writer.append(125, 0x420, 0, b"scc-data"))
      self.assertFalse(writer.append(126, 0x123, 0, b"ignored"))
      capture = writer.finalize()
      self.assertIsNotNone(capture)
      self.assertEqual(list(iter_records(capture)), [
        (123, 0x500, 1, b"\x01\x02\x03\x04\x05\x06\x07\x08"),
        (124, 0x238, 1, b"classic!"),
        (125, 0x420, 0, b"scc-data"),
      ])

  def test_radar_bus_ranges_are_kept_only_from_bus_1(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = RadarCaptureWriter(Path(temp_dir), wall_time=1000)
      for address in (0x202, 0x25A, 0x26E, 0x690, 0x240):
        self.assertTrue(writer.append(1, address, 1, b"radarbus"))
      # Same IDs on bus 0 (e.g. 0x240, MDPS12 0x251) and their echoes are unrelated frames.
      for address, source in ((0x240, 0), (0x251, 0), (0x251, 194), (0x25A, 0), (0x238, 130)):
        self.assertFalse(writer.append(2, address, source, b"notradar"))
      self.assertTrue(writer.append(3, 0x420, 128, b"scc-echo"))
      capture = writer.finalize()
      self.assertEqual([(address, source) for _, address, source, _ in iter_records(capture)],
                       [(0x202, 1), (0x25A, 1), (0x26E, 1), (0x690, 1), (0x240, 1), (0x420, 128)])

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
      SimpleNamespace(vEgo=10.0, vCruise=60.0, steeringAngleDeg=2.0, aEgo=-0.3, gasPressed=False, brakePressed=True,
                      standstill=False, steeringPressed=False, leftBlindspot=True, rightBlindspot=False),
      SimpleNamespace(position=xy, laneLines=IntegerOnlyList([xy] * 4),
                      laneLineProbs=IntegerOnlyList([0.9] * 4), leadsV3=IntegerOnlyList([model_lead])),
      SimpleNamespace(points=IntegerOnlyList([radar_point])),
      SimpleNamespace(leadOne=lead, leadTwo=lead),
      SimpleNamespace(longActive=True, enabled=True, actuators=SimpleNamespace(accel=-0.5)),
      SimpleNamespace(tFollow=1.1, desiredDistance=16.5),
      {"calib": [0.0, 0.02, -0.01, 1.3], "cam": [1344, 760, 1141.5]},
      {"PathOffset": 0.0, "CameraYawTrimDeg": -10.0},
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
    self.assertEqual((saved["a_ego"], saved["brake_pressed"], saved["left_blindspot"]), (-0.3, True, True))
    self.assertEqual((saved["t_follow"], saved["desired_distance"]), (1.1, 16.5))
    self.assertEqual((saved["calib"], saved["cam"]), ([0.0, 0.02, -0.01, 1.3], [1344, 760, 1141.5]))
    self.assertEqual(saved["settings"], {"PathOffset": 0.0, "CameraYawTrimDeg": -10.0})
    self.assertEqual(saved["v_cruise"], 60.0)

  def test_scene_capture_tolerates_missing_optional_fields(self):
    xy = SimpleNamespace(x=IntegerOnlyList([0.0]), y=IntegerOnlyList([0.0]))
    lead = SimpleNamespace(status=False, radar=False, radarTrackId=-1, dRel=0.0, yRel=0.0, vRel=0.0,
                           vLead=0.0, dPath=0.0, modelProb=0.0)
    frame = build_scene_frame(
      1, SimpleNamespace(vEgo=0.0, steeringAngleDeg=0.0),
      SimpleNamespace(position=xy, laneLines=IntegerOnlyList([]), laneLineProbs=IntegerOnlyList([]),
                      leadsV3=IntegerOnlyList([])),
      SimpleNamespace(points=IntegerOnlyList([])), SimpleNamespace(leadOne=lead, leadTwo=lead),
      SimpleNamespace(longActive=False, enabled=False, actuators=SimpleNamespace(accel=0.0)),
    )
    self.assertIsNone(frame["a_ego"])
    self.assertIsNone(frame["t_follow"])
    self.assertIsNone(frame["left_blindspot"])
    self.assertIsNone(frame["calib"])
    self.assertNotIn("settings", frame)

  def test_prune_spool_removes_oldest_uploaded_bundles_first(self):
    from tools.c4_diagnostics.auto_upload import prune_spool
    with tempfile.TemporaryDirectory() as temp_dir:
      root = Path(temp_dir)
      for name in ("20260901T000000Z-a", "20260902T000000Z-b", "20260903T000000Z-c"):
        (root / f"{name}.c4radar").write_bytes(b"r" * 100)
        (root / f"{name}.c4scene").write_bytes(b"s" * 100)
      (root / "radar-inventory-x.json").write_text("{}", encoding="utf-8")
      state = {"uploaded": {"20260902T000000Z-b.c4radar": {}, "20260903T000000Z-c.c4radar": {}}}
      self.assertEqual(prune_spool(root, state, max_bytes=450), 200)
      names = sorted(path.name for path in root.iterdir())
      # b (oldest uploaded) goes first; a is still pending upload and stays.
      self.assertNotIn("20260902T000000Z-b.c4radar", names)
      self.assertIn("20260901T000000Z-a.c4radar", names)
      self.assertIn("radar-inventory-x.json", names)
      self.assertEqual(prune_spool(root, state, max_bytes=100), 400)
      self.assertEqual(sorted(path.name for path in root.iterdir()), ["radar-inventory-x.json"])

  def test_upload_includes_scene_companion(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      root = Path(temp_dir)
      capture = root / "capture.c4radar"
      scene = root / "capture.c4scene"
      inventory = root / "capture.c4can.json"
      video = root / "capture.qcamera.h264"
      video_metadata = root / "capture.qcamera.json"
      capture.write_bytes(b"radar")
      scene.write_text('{"schema":"c4-scene-v1"}\n', encoding="utf-8")
      inventory.write_text('{"schema":"c4-can-inventory-v1"}\n', encoding="utf-8")
      video.write_bytes(b"h264")
      video_metadata.write_text('{"schema":"c4-qcamera-video-v1"}\n', encoding="utf-8")
      event = root / "capture.c4event.json"
      event.write_text('{"schema":"c4-event-v1"}', encoding="utf-8")
      state_path = root / "state.json"
      state = {"schema": 2, "started_at": 1, "uploaded": {}}
      with patch("tools.c4_diagnostics.auto_upload.upload", return_value={"upload_id": "id"}) as send:
        upload_one(UploadConfig("https://example.com", "key"), "c4-001", state, state_path, capture)
      self.assertEqual(send.call_args.args[3], [capture, scene, inventory, video, video_metadata, event])

  def test_qcamera_capture_starts_on_header_and_stops_after_thirty_seconds(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = QCameraCaptureWriter(Path(temp_dir), "capture")
      packet = lambda header, data: SimpleNamespace(header=header, data=data, width=512, height=256)
      self.assertFalse(writer.append(1_000_000_000, packet(b"", b"p")))
      self.assertTrue(writer.append(2_000_000_000, packet(b"header", b"key")))
      self.assertTrue(writer.append(32_000_000_000, packet(b"", b"delta")))
      self.assertTrue(writer.complete())
      video, metadata = writer.finalize()
      saved = __import__("json").loads(metadata.read_text(encoding="utf-8"))
      self.assertEqual(video.read_bytes(), b"headerkeydelta")
      self.assertEqual(saved["duration_s"], 30.0)
      self.assertEqual(saved["frames"], 2)

  def test_periodic_bundles_are_video_bundles_every_ten_minutes(self):
    capture, video = None, None
    starts = []
    now = 1000.0
    while now < 1000.0 + 25 * 60:
      start, with_video, capture, video = plan_bundle_start(now, capture, video)
      if start:
        starts.append((round((now - 1000.0) / 60), with_video))
        now += 40.0  # one bundle is recorded before the next plan is checked
      else:
        now += 0.5
    self.assertEqual(starts, [(0, True), (10, True), (20, True)])

  def test_new_drive_starts_with_video_bundle_immediately(self):
    self.assertEqual(plan_bundle_start(50.0, None, None), (True, True, 650.0, 650.0))
    self.assertEqual(plan_bundle_start(649.0, 650.0, 650.0), (False, False, 650.0, 650.0))
    self.assertEqual(plan_bundle_start(650.0, 650.0, 650.0), (True, True, 1250.0, 1250.0))

  def test_representative_video_uses_ten_minutes_and_thirty_seconds(self):
    self.assertEqual(REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS, 600)
    self.assertEqual(VIDEO_DURATION_SECONDS, 30.0)
    self.assertFalse(representative_video_due(100.0, None))
    next_time = next_representative_video_time(100.0)
    self.assertFalse(representative_video_due(699.999, next_time))
    self.assertTrue(representative_video_due(700.0, next_time))

  def test_can_inventory_keeps_incoming_counts_and_change_mask(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      writer = CanInventoryWriter(Path(temp_dir), "capture")
      self.assertTrue(writer.append(1_000_000_000, 0x123, 1, b"\x10\x20"))
      self.assertTrue(writer.append(2_000_000_000, 0x123, 1, b"\x11\x20"))
      self.assertFalse(writer.append(2_000_000_000, 0x420, 128, b"\x01"))
      output = writer.finalize()
      saved = __import__("json").loads(output.read_text(encoding="utf-8"))
    self.assertTrue(saved["incoming_only"])
    self.assertFalse(saved["transmit_performed"])
    self.assertEqual(saved["total_frames"], 2)
    self.assertEqual(saved["bus_counts"], {"1": 2})
    self.assertEqual(saved["addresses"]["1:0x123:2"], {
      "count": 2, "rate_hz": 1.0, "first_hex": "1020", "last_hex": "1120",
      "varying_mask_hex": "0100",
    })

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
    self.assertLess(MAX_CAPTURE_BYTES + MAX_SCENE_BYTES + MAX_INVENTORY_BYTES + MAX_QCAMERA_BYTES, MAX_TOTAL_BYTES)


if __name__ == "__main__":
  unittest.main()
