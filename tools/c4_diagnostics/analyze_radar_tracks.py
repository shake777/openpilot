# C4 CAN 원본에서 실제 수신된 Mando 레이더 트랙 후보만 분리해 통계를 낸다.
import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from tools.c4_diagnostics.radar_capture import iter_records


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
  for address, frames in sorted(addresses.items()):
    intervals = [(frames[i][0] - frames[i - 1][0]) / 1e6 for i in range(1, len(frames))]
    by_address[f'0x{address:03x}'] = {
      'bank': 'required_first_32' if address < 0x520 else 'optional_upper_32',
      'count': len(frames),
      'median_interval_ms': statistics.median(intervals) if intervals else None,
      'stddev_interval_ms': statistics.pstdev(intervals) if intervals else None,
      'distinct_payloads': len({data for _, data in frames}),
    }
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
    'verdict': 'no_radar_tracks_present' if not addresses else 'track_frames_present_unvalidated',
    'interpretation': '0x500-0x53f is a numeric range, not proof of radar tracks; verify bus, DLC, payload and DBC',
  }


if __name__ == '__main__':
  parser = argparse.ArgumentParser(description='Analyze received bus-1 Mando track candidates in a C4 radar capture')
  parser.add_argument('capture', type=Path)
  args = parser.parse_args()
  print(json.dumps(analyze(args.capture), ensure_ascii=False, sort_keys=True, indent=2))
