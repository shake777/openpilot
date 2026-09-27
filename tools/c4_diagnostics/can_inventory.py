# 운행 중 수신 CAN 주소별 빈도와 변화 바이트를 제한된 JSON으로 집계하는 모듈
import json
import math
import os
from collections import Counter
from pathlib import Path


SCHEMA = "c4-can-inventory-v1"
MAX_ENTRIES = 2048
MAX_INVENTORY_BYTES = 256 * 1024


class CanInventoryWriter:
  def __init__(self, spool_dir: Path, capture_name: str):
    self.partial_path = spool_dir / f"{capture_name}.c4can.json.partial"
    self.entries = {}
    self.bus_counts = Counter()
    self.total_frames = 0
    self.omitted_frames = 0

  def append(self, mono_time: int, address: int, source: int, data: bytes) -> bool:
    if not 0 <= source < 0x80 or not data:
      return False
    self.total_frames += 1
    self.bus_counts[str(source)] += 1
    key = f"{source}:0x{address:03x}:{len(data)}"
    entry = self.entries.get(key)
    if entry is None:
      if len(self.entries) >= MAX_ENTRIES:
        self.omitted_frames += 1
        return False
      entry = {"count": 0, "first_ns": int(mono_time), "last_ns": int(mono_time),
               "first_hex": data.hex(), "last_hex": data.hex(),
               "and": bytearray(data), "or": bytearray(data)}
      self.entries[key] = entry
    entry["count"] += 1
    entry["last_ns"] = int(mono_time)
    entry["last_hex"] = data.hex()
    for index, value in enumerate(data):
      entry["and"][index] &= value
      entry["or"][index] |= value
    return True

  @staticmethod
  def _public_entry(entry):
    elapsed = (entry["last_ns"] - entry["first_ns"]) / 1e9
    rate = (entry["count"] - 1) / elapsed if entry["count"] > 1 and elapsed > 0 else None
    return {"count": entry["count"], "rate_hz": round(rate, 3) if rate is not None and math.isfinite(rate) else None,
            "first_hex": entry["first_hex"], "last_hex": entry["last_hex"],
            "varying_mask_hex": bytes(a ^ b for a, b in zip(entry["and"], entry["or"])).hex()}

  def finalize(self) -> Path | None:
    if self.total_frames == 0:
      return None
    addresses = {key: self._public_entry(value) for key, value in sorted(self.entries.items())}
    report = {"schema": SCHEMA, "transmit_performed": False, "incoming_only": True,
              "total_frames": self.total_frames, "bus_counts": dict(self.bus_counts),
              "omitted_frames": self.omitted_frames, "trimmed_keys": 0, "addresses": addresses}
    encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    while len(encoded) > MAX_INVENTORY_BYTES and addresses:
      key = min(addresses, key=lambda item: (addresses[item]["count"], item))
      del addresses[key]
      report["trimmed_keys"] += 1
      encoded = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    self.partial_path.write_bytes(encoded)
    os.chmod(self.partial_path, 0o600)
    final_path = self.partial_path.with_suffix("")
    os.replace(self.partial_path, final_path)
    return final_path
