# SCC11 원본 해석이 실제 DBC 비트 정의와 수신 버스를 따르는지 검증한다.
import tempfile
import unittest
from pathlib import Path

from tools.c4_diagnostics.analyze_scc11 import analyze, decode_scc11
from tools.c4_diagnostics.radar_capture import MAGIC, encode_record


def pack_scc11(status: int, distance_raw: int, speed_raw: int, lateral_raw: int) -> bytes:
  bits = (status << 22) | (lateral_raw << 24) | (distance_raw << 33) | (speed_raw << 44)
  return bits.to_bytes(8, 'little')


class TestAnalyzeScc11(unittest.TestCase):
  def test_decode_uses_current_generic_dbc_layout(self):
    decoded = decode_scc11(pack_scc11(1, 423, 1676, 207))
    self.assertEqual(decoded, {
      'status': 1,
      'distance_m': 42.3,
      'relative_speed_mps': -2.4,
      'lateral_position_m': 0.7,
    })

  def test_received_bus_excludes_echo_and_other_bus(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      path = Path(temp_dir) / 'sample.c4radar'
      lead = pack_scc11(1, 423, 1676, 207)
      no_lead = pack_scc11(0, 0, 1700, 200)
      path.write_bytes(MAGIC + b''.join((
        encode_record(0, 0x420, 0, lead),
        encode_record(50_000_000, 0x420, 0, no_lead),
        encode_record(50_000_000, 0x420, 128, lead),
        encode_record(50_000_000, 0x420, 2, lead),
        encode_record(100_000_000, 0x420, 0, b'\x00'),
      )))
      report = analyze(path)
      camera_bus_report = analyze(path, 2)

    self.assertEqual(report['scc11_source_counts'], {'0': 3, '2': 1, '128': 1})
    self.assertEqual(report['received_frames'], 2)
    self.assertEqual(report['invalid_dlc'], 1)
    self.assertEqual(report['usable_lead_samples'], 1)
    self.assertEqual(report['median_interval_ms'], 50)
    self.assertEqual(report['distance_range_m'], [42.3, 42.3])
    self.assertEqual(report['relative_speed_range_mps'], [-2.4, -2.4])
    self.assertEqual(camera_bus_report['usable_lead_samples'], 1)

  def test_invalid_bus_is_rejected(self):
    with self.assertRaises(ValueError):
      analyze(Path('unused.c4radar'), 128)


if __name__ == '__main__':
  unittest.main()
