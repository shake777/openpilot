# C4 CAN 원본에서 실제 수신된 Mando 레이더 트랙 후보만 분리해 통계를 낸다.
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from tools.c4_diagnostics.radar_capture import iter_records


def decode_mando(data: bytes) -> dict:
  """Decode the legacy Mando layout; a plausible decode is not ECU identification."""
  if len(data) != 8:
    raise ValueError('Mando track payload must be 8 bytes')
  word = int.from_bytes(data, 'big')
  result = {}
  # Motorola start bit, width, signed, scale from hyundai_kia_mando_front_radar.py.
  for name, start, width, signed, scale in (
    ('STATE', 15, 3, False, 1), ('LONG_DIST', 18, 11, False, 0.1),
    ('AZIMUTH', 12, 10, True, 0.2), ('REL_SPEED', 53, 14, True, 0.01),
    ('REL_ACCEL', 33, 10, True, 0.02), ('COUNTER', 38, 1, False, 1),
  ):
    shift = 56 - 8 * (start // 8) + start % 8 - width + 1
    value = (word >> shift) & ((1 << width) - 1)
    if signed and value & (1 << (width - 1)):
      value -= 1 << width
    result[name] = value * scale
  return result


def analyze(path: Path) -> dict:
  stages = Counter()
  sources = Counter()
  addresses = defaultdict(list)
  received_range = defaultdict(lambda: defaultdict(Counter))
  for mono_time, address, source, data in iter_records(path):
    stages['all'] += 1
    sources[source] += 1
    if source >= 0x80:
      continue
    stages['received'] += 1
    if 0x500 <= address <= 0x53f:
      received_range[source][address][len(data)] += 1
    if source != 1:
      continue
    stages['bus_1'] += 1
    if not 0x500 <= address <= 0x53f:
      continue
    stages['track_range'] += 1
    if len(data) != 8:
      continue
    stages['track_dlc'] += 1
    addresses[address].append((mono_time, data))

  by_address = {}
  events = []
  for address, frames in sorted(addresses.items()):
    frames.sort(key=lambda frame: frame[0])
    intervals = [(frames[i][0] - frames[i - 1][0]) / 1e6 for i in range(1, len(frames))]
    decoded = [decode_mando(data) for _, data in frames]
    # Match the actual non-CANFD radar interface, not the document's STATE != 0.
    valid = [msg for msg in decoded if msg['STATE'] in (3, 4)]
    events.extend((stamp, address, msg['STATE'] in (3, 4)) for (stamp, _), msg in zip(frames, decoded))
    by_address[f'0x{address:03x}'] = {
      'bank': 'required_first_32' if address < 0x520 else 'optional_upper_32',
      'count': len(frames),
      'median_interval_ms': statistics.median(intervals) if intervals else None,
      'stddev_interval_ms': statistics.pstdev(intervals) if intervals else None,
      'distinct_payloads': len({data for _, data in frames}),
      'state_counts': dict(sorted(Counter(str(msg['STATE']) for msg in decoded).items())),
      'valid_state_frames': len(valid),
      'distance_range_m': [min(msg['LONG_DIST'] for msg in valid), max(msg['LONG_DIST'] for msg in valid)] if valid else None,
      'relative_speed_range_mps': [min(msg['REL_SPEED'] for msg in valid), max(msg['REL_SPEED'] for msg in valid)] if valid else None,
      'expected_interval_ms': 50,
    }
  # Evaluate a trailing 50 ms window at each distinct timestamp. An invalid frame
  # clears its slot immediately. Distinct slots at equal distances remain distinct.
  by_time = defaultdict(list)
  for stamp, address, valid in events:
    by_time[stamp].append((address, valid))
  recent = {}
  concurrency = Counter()
  for stamp, updates in sorted(by_time.items()):
    recent = {address: seen for address, seen in recent.items() if stamp - seen < 50_000_000}
    for address, valid in updates:
      if valid:
        recent[address] = stamp
      else:
        recent.pop(address, None)
    concurrency[len(recent)] += 1
  max_candidates = max(concurrency, default=0)
  return {
    'file': path.name,
    'stages': {name: stages[name] for name in ('all', 'received', 'bus_1', 'track_range', 'track_dlc')},
    'source_counts': {str(source): count for source, count in sorted(sources.items())},
    'received_range_by_bus': {
      str(source): {
        f'0x{address:03x}': {
          'count': sum(dlc_counts.values()),
          'dlc_counts': {str(dlc): count for dlc, count in sorted(dlc_counts.items())},
        }
        for address, dlc_counts in sorted(source_addresses.items())
      }
      for source, source_addresses in sorted(received_range.items())
    },
    'addresses': by_address,
    'candidate_observation': 'multiple_slots' if max_candidates > 1 else 'single_slot' if max_candidates else 'no_valid_slots',
    'concurrency': {
      'window_ms': 50, 'max_candidate_slots': max_candidates,
      'timestamp_sample_counts': {str(count): samples for count, samples in sorted(concurrency.items())},
      'interpretation': 'Overlapping timestamp samples are not independent radar cycles or confirmed physical targets.',
    },
    'verdict': 'no_radar_tracks_present' if not addresses else 'track_frames_present_unvalidated',
    'interpretation': 'Capture-local bus-1 candidates only; absence does not prove ECU incapability. DBC decoding does not confirm physical targets.',
  }


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Analyze received bus-1 Mando track candidates in a C4 radar capture')
  parser.add_argument('capture', type=Path)
  args = parser.parse_args()
  print(json.dumps(analyze(args.capture), ensure_ascii=False, sort_keys=True, indent=2))
