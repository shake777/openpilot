# K7 레이더 활성화 후보의 식별자 제한과 백업 복원을 검증한다.
import json
import tempfile
import unittest
from pathlib import Path

from openpilot.selfdrive.debug.car import hyundai_enable_radar_points as radar


class K7RadarTrialTests(unittest.TestCase):
  def test_firmware_identity_is_exactly_scoped(self):
    self.assertTrue(radar.is_k7_experimental_firmware(
      b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         '))
    self.assertFalse(radar.is_k7_experimental_firmware(
      b'YG__ SCC FHCUP      1.00 1.03 99110-F6000         '))
    self.assertFalse(radar.is_k7_experimental_firmware(
      b'DN8_ SCC FHCUP      1.00 1.02 99110-L0000         '))

  def test_backup_round_trip_and_mismatch_rejection(self):
    firmware = b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         '
    original = radar.K7_EXPERIMENTAL_CONFIG.default_config
    with tempfile.TemporaryDirectory() as temp_dir:
      backup_path = Path(temp_dir) / 'backup.json'
      radar.save_k7_backup(firmware, original, backup_path)
      self.assertEqual(radar.load_k7_backup(backup_path), original)
      self.assertEqual(json.loads(backup_path.read_text(encoding='utf-8'))['original_config_hex'], '0002000000')
      with self.assertRaises(ValueError):
        radar.save_k7_backup(firmware, b'\x99' * 5, backup_path)


if __name__ == '__main__':
  unittest.main()
