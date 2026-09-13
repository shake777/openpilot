# C4 레이더 캡처 형식과 48시간 자동 전송 상태를 검증하는 테스트
import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.auto_upload import deterministic_upload_id, is_active, load_or_start_state, pending_captures
from tools.c4_diagnostics.radar_capture import RadarCaptureWriter, iter_records


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

  def test_activation_expires_after_48_hours(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      state_path = Path(temp_dir) / "state.json"
      state = load_or_start_state(state_path, 48, now=1000)
      self.assertTrue(is_active(state, now=1000 + 48 * 3600 - 1))
      self.assertFalse(is_active(state, now=1000 + 48 * 3600))
      self.assertEqual(load_or_start_state(state_path, 48, now=2000), state)

  def test_pending_and_upload_id_are_stable(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      spool = Path(temp_dir)
      first = spool / "001.c4radar"
      second = spool / "002.c4radar"
      first.write_bytes(b"first")
      second.write_bytes(b"second")
      state = {"uploaded": {first.name: {}}}
      self.assertEqual(pending_captures(spool, state), [second])
      self.assertEqual(
        deterministic_upload_id("c4-001", second, "abc"),
        deterministic_upload_id("c4-001", second, "abc"),
      )


if __name__ == "__main__":
  unittest.main()
