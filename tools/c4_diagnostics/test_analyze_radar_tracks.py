# C4 레이더 원본 분석에서 버스·에코·DLC 필터가 지켜지는지 검증한다.
import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.analyze_radar_tracks import analyze, decode_mando
from tools.c4_diagnostics.radar_capture import MAGIC, encode_record


class TestAnalyzeRadarTracks(unittest.TestCase):
  def analyze_records(self, records):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'sample.c4radar'
      path.write_bytes(MAGIC + b''.join(encode_record(*record) for record in records))
      return analyze(path)

  def test_golden_motorola_payload(self):
    # Independently hand-packed bytes: state=3, distance raw=1000,
    # azimuth raw=-6, speed raw=-150, acceleration raw=10, counter=1.
    decoded = decode_mando(bytes.fromhex('007fd3e8400a3f6a'))
    self.assertEqual(decoded['STATE'], 3)
    self.assertEqual(decoded['LONG_DIST'], 100)
    self.assertAlmostEqual(decoded['AZIMUTH'], -1.2)
    self.assertEqual(decoded['REL_SPEED'], -1.5)
    self.assertEqual(decoded['REL_ACCEL'], 0.2)
    self.assertEqual(decoded['COUNTER'], 1)
    with self.assertRaises(ValueError):
      decode_mando(b'\0' * 4)

  def test_field_definitions_match_dbc_generator(self):
    generator = Path(__file__).resolve().parents[2] / 'opendbc_repo/opendbc/dbc/generator/hyundai/hyundai_kia_mando_front_radar.py'
    content = generator.read_text(encoding='utf-8')
    for definition in ('STATE : 15|3@0+ (1,0)', 'LONG_DIST : 18|11@0+ (0.1,0)',
                       'AZIMUTH : 12|10@0- (0.2,0)', 'REL_SPEED : 53|14@0- (0.01,0)',
                       'REL_ACCEL : 33|10@0- (0.02,0)', 'COUNTER : 38|1@0+ (1,0)'):
      self.assertIn(definition, content)

  def test_equal_distance_distinct_slots_are_not_merged(self):
    result = self.analyze_records([
      (0, 0x501, 1, bytes.fromhex('007fd3e8400a3f6a')),
      (49_000_000, 0x502, 1, bytes.fromhex('009fd3e8400a3f6a')),
    ])
    self.assertEqual(result['candidate_observation'], 'multiple_slots')
    self.assertEqual(result['concurrency']['max_candidate_slots'], 2)
    self.assertEqual(result['verdict'], 'track_frames_present_unvalidated')

  def test_invalid_state_clears_slot_and_window_expires(self):
    valid = bytes.fromhex('007fd3e8400a3f6a')
    invalid = bytes.fromhex('003fd3e8400a3f6a')  # State 1 is not valid.
    result = self.analyze_records([(0, 0x500, 1, valid), (1_000_000, 0x500, 1, invalid),
                                   (2_000_000, 0x501, 1, valid), (52_000_000, 0x520, 1, valid)])
    self.assertEqual(result['concurrency']['max_candidate_slots'], 1)
    self.assertEqual(result['addresses']['0x500']['valid_state_frames'], 1)
    self.assertEqual(result['addresses']['0x500']['state_counts'], {'1': 1, '3': 1})

  def test_empty_and_inactive_frames_do_not_confirm_targets(self):
    self.assertEqual(self.analyze_records([])['candidate_observation'], 'no_valid_slots')
    result = self.analyze_records([(0, 0x500, 1, bytes.fromhex('003fd3e8400a3f6a'))])
    self.assertEqual(result['candidate_observation'], 'no_valid_slots')
    self.assertEqual(result['verdict'], 'track_frames_present_unvalidated')

  def test_truncated_capture_is_not_reported_as_absence(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / 'sample.c4radar'
      path.write_bytes(MAGIC + encode_record(0, 0x500, 1, b'\0' * 8)[:-1])
      with self.assertRaisesRegex(ValueError, 'truncated'):
        analyze(path)

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
      self.assertEqual(result['received_range_by_bus'], {
        '0': {'0x500': {'count': 1, 'dlc_counts': {'1': 1}}},
        '1': {'0x507': {'count': 1, 'dlc_counts': {'4': 1}}},
      })
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
