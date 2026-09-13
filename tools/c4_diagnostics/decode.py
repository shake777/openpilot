#!/usr/bin/env python3
# C4 레이더 이진 캡처를 사람이 확인할 수 있는 CSV로 변환하는 도구
import argparse
import csv
from pathlib import Path

from tools.c4_diagnostics.radar_capture import iter_records


def main() -> int:
  parser = argparse.ArgumentParser(description="Decode a C4 radar capture to CSV")
  parser.add_argument("capture", type=Path)
  parser.add_argument("--output", type=Path)
  args = parser.parse_args()
  output_path = args.output or args.capture.with_suffix(".csv")
  with output_path.open("w", newline="", encoding="utf-8") as output:
    writer = csv.writer(output)
    writer.writerow(("mono_time_ns", "address_hex", "source", "data_hex"))
    for mono_time, address, source, data in iter_records(args.capture):
      writer.writerow((mono_time, f"0x{address:X}", source, data.hex()))
  print(output_path)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
