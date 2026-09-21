# C4 레이더 원본 분석에서 버스·에코·DLC 필터가 지켜지는지 검증한다.
import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.analyze_radar_tracks import analyze
from tools.c4_diagnostics.radar_capture import MAGIC, encode_record


class TestAnalyzeRadarTracks(unittest.TestCase):
  def test_echo_and_wrong_bus_or_dlc_are_not_tracks(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'sample.c4radar'
      path.write_bytes(MAGIC + b''.join((
        encode_record(1, 0x500, 0, b'\x01'),
        encode_record(2, 0x500, 130, b'\x00' * 8),
        encode_record(3, 0x420, 1, b'\x00' * 8),
        encode_record(4, 0x507, 1, b'\x00' * 4),
      )))
      result = analyze(path)
      self.assertEqual(result['stages'], {'all': 4, 'received': 3, 'bus_1': 2, 'track_range': 1, 'track_dlc': 0})
      self.assertEqual(result['source_counts'], {'0': 1, '1': 2, '130': 1})
      self.assertEqual(result['verdict'], 'no_radar_tracks_present')

  def test_upper_bank_is_not_discarded_or_declared_confirmed(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'sample.c4radar'
      path.write_bytes(MAGIC + b''.join((
        encode_record(0, 0x520, 1, b'\x00' * 8),
        encode_record(50_000_000, 0x520, 1, b'\x01' * 8),
      )))
      result = analyze(path)
      self.assertEqual(result['verdict'], 'track_frames_present_unvalidated')
      self.assertEqual(result['addresses']['0x520']['bank'], 'optional_upper_32')
      self.assertEqual(result['addresses']['0x520']['median_interval_ms'], 50)
      self.assertEqual(result['addresses']['0x520']['distinct_payloads'], 2)


if __name__ == '__main__':
  unittest.main()
