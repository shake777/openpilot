# C4 CAN 캡처에서 순정 SCC11 대상값을 DBC 정의에 따라 읽기 전용으로 분석한다.
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from tools.c4_diagnostics.radar_capture import iter_records


SCC11_ADDR = 0x420
SCC12_ADDR = 0x421
SCC11_LEN = 8


def decode_scc11(data: bytes) -> dict:
  if len(data) != SCC11_LEN:
    raise ValueError('SCC11 requires 8 data bytes')
  bits = int.from_bytes(data, 'little')
  return {
    'obj_valid': bool((bits >> 16) & 1),
    'status': (bits >> 22) & 0x3,
    'distance_m': round(((bits >> 33) & 0x7ff) * 0.1, 1),
    'relative_speed_mps': round(((bits >> 44) & 0xfff) * 0.1 - 170, 1),
    'lateral_position_m': round(((bits >> 24) & 0x1ff) * 0.1 - 20, 1),
  }


def decode_scc12(data: bytes) -> dict:
  if len(data) != SCC11_LEN:
    raise ValueError('SCC12 requires 8 data bytes')
  bits = int.from_bytes(data, 'little')
  return {
    'a_req_raw_mps2': round(((bits >> 24) & 0x7ff) * 0.01 - 10.23, 2),
    'a_req_value_mps2': round(((bits >> 37) & 0x7ff) * 0.01 - 10.23, 2),
  }


def analyze(path: Path, bus: int = 0) -> dict:
  if not 0 <= bus < 0x80:
    raise ValueError('bus must be a received CAN source')
  source_counts = Counter()
  status_counts = Counter()
  selected_times = []
  valid = []
  invalid_dlc = 0
  obj_valid_frames = 0
  scc12_values = []
  scc12_invalid_dlc = 0
  for mono_time, address, source, data in iter_records(path):
    if address == SCC12_ADDR and source == bus:
      if len(data) == SCC11_LEN:
        scc12_values.append(decode_scc12(data))
      else:
        scc12_invalid_dlc += 1
      continue
    if address != SCC11_ADDR:
      continue
    source_counts[source] += 1
    if source != bus:
      continue
    if len(data) != SCC11_LEN:
      invalid_dlc += 1
      continue
    selected_times.append(mono_time)
    decoded = decode_scc11(data)
    status_counts[decoded['status']] += 1
    obj_valid_frames += decoded['obj_valid']
    if decoded['obj_valid'] and decoded['status'] and 0 < decoded['distance_m'] < 150:
      valid.append(decoded)

  intervals_ms = [(later - earlier) / 1e6 for earlier, later in zip(selected_times, selected_times[1:])]
  return {
    'file': path.name,
    'selected_bus': bus,
    'scc11_source_counts': {str(source): count for source, count in sorted(source_counts.items())},
    'received_frames': len(selected_times),
    'invalid_dlc': invalid_dlc,
    'obj_valid_frames': obj_valid_frames,
    'scc12_received_frames': len(scc12_values),
    'scc12_invalid_dlc': scc12_invalid_dlc,
    'scc12_a_req_raw_range_mps2': [min(v['a_req_raw_mps2'] for v in scc12_values), max(v['a_req_raw_mps2'] for v in scc12_values)] if scc12_values else None,
    'scc12_a_req_value_range_mps2': [min(v['a_req_value_mps2'] for v in scc12_values), max(v['a_req_value_mps2'] for v in scc12_values)] if scc12_values else None,
    'status_counts': {str(status): count for status, count in sorted(status_counts.items())},
    'usable_lead_samples': len(valid),
    'median_interval_ms': statistics.median(intervals_ms) if intervals_ms else None,
    'distance_range_m': [min(v['distance_m'] for v in valid), max(v['distance_m'] for v in valid)] if valid else None,
    'relative_speed_range_mps': [min(v['relative_speed_mps'] for v in valid), max(v['relative_speed_mps'] for v in valid)] if valid else None,
    'lateral_position_range_m': [min(v['lateral_position_m'] for v in valid), max(v['lateral_position_m'] for v in valid)] if valid else None,
    'interpretation': 'SCC11 selected-lead samples only; not evidence of multi-object radar tracks or safety validation',
  }


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Read-only SCC11 statistics from a C4 radar capture')
  parser.add_argument('capture', type=Path)
  parser.add_argument('--bus', type=int, default=0, help='received CAN source containing SCC11 (default: 0)')
  args = parser.parse_args()
  print(json.dumps(analyze(args.capture, args.bus), ensure_ascii=False, sort_keys=True, indent=2))
