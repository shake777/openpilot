# K7 레이더 관련 CAN 원본 바이트를 작은 이진 파일로 보존하고 해석하는 모듈
import os
import struct
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Iterator


MAGIC = b"C4RADAR1\n"
RECORD_HEADER = struct.Struct("<QIBB")
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_DATA_BYTES = 64
SCC_ADDRESSES = frozenset((0x389, 0x420, 0x421, 0x50A))
CLASSIC_TRACK_START = 0x238
CLASSIC_TRACK_END = 0x255
MAX_PENDING_DIAGNOSTICS = 256
RADAR_BUS = 1
# Other K7 radar-bus messages still being decoded: 0x202/0x203 carry a millisecond
# counter, 0x25A-0x25E look like up to ten raw detections, the rest are unknown.
RADAR_AUX_ADDRESSES = frozenset((*range(0x201, 0x20E), *range(0x25A, 0x25F), *range(0x266, 0x270), 0x690, 0x691))


def is_radar_address(address: int, source: int | None = None) -> bool:
  # Radar-bus ranges are kept only from bus 1; on bus 0 the same IDs are unrelated
  # powertrain frames (e.g. 0x240, MDPS12 0x251) that used to fill a quarter of each capture.
  if CLASSIC_TRACK_START <= address <= CLASSIC_TRACK_END or address in RADAR_AUX_ADDRESSES:
    return source is None or source == RADAR_BUS
  # Radar diagnostic frames (0x7D0/0x7D8) are no longer captured (probes removed 2026-10-03).
  return 0x500 <= address <= 0x53F or address in SCC_ADDRESSES


def capture_can_frame(writer, pending_diagnostics: deque, mono_time: int, address: int, source: int, data: bytes) -> None:
  if writer is not None:
    while pending_diagnostics:
      writer.append(*pending_diagnostics.popleft())
    writer.append(mono_time, address, source, data)


def encode_record(mono_time: int, address: int, source: int, data: bytes) -> bytes:
  if not 0 <= source <= 255:
    raise ValueError("CAN source is outside the supported range")
  if not 0 <= address <= 0x1FFFFFFF:
    raise ValueError("CAN address is outside the supported range")
  if not 0 < len(data) <= MAX_DATA_BYTES:
    raise ValueError("CAN payload length is outside the supported range")
  return RECORD_HEADER.pack(mono_time, address, source, len(data)) + data


def iter_records(path: Path) -> Iterator[tuple[int, int, int, bytes]]:
  with path.open("rb") as stream:
    if stream.read(len(MAGIC)) != MAGIC:
      raise ValueError("not a C4 radar capture")
    while header := stream.read(RECORD_HEADER.size):
      if len(header) != RECORD_HEADER.size:
        raise ValueError("truncated C4 radar record header")
      mono_time, address, source, data_length = RECORD_HEADER.unpack(header)
      data = stream.read(data_length)
      if len(data) != data_length:
        raise ValueError("truncated C4 radar record payload")
      yield mono_time, address, source, data


class RadarCaptureWriter:
  def __init__(self, spool_dir: Path, wall_time: float | None = None, capture_id: str | None = None):
    self.spool_dir = spool_dir
    self.spool_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(self.spool_dir, 0o700)
    self.started_at = wall_time if wall_time is not None else time.time()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.started_at))
    self.capture_name = f"{stamp}-{capture_id or uuid.uuid4().hex}"
    self.partial_path = self.spool_dir / f"{self.capture_name}.c4radar.partial"
    self.stream = self.partial_path.open("wb")
    self.stream.write(MAGIC)
    self.size = len(MAGIC)
    self.records = 0

  def append(self, mono_time: int, address: int, source: int, data: bytes) -> bool:
    if not is_radar_address(address, source):
      return False
    record = encode_record(mono_time, address, source, data)
    self.stream.write(record)
    self.size += len(record)
    self.records += 1
    return True

  def should_rotate(self, now: float, max_age_seconds: float = 60.0) -> bool:
    return self.size >= MAX_CAPTURE_BYTES or now - self.started_at >= max_age_seconds

  def finalize(self) -> Path | None:
    if self.stream.closed:
      return None
    self.stream.flush()
    os.fsync(self.stream.fileno())
    self.stream.close()
    if self.records == 0:
      self.partial_path.unlink(missing_ok=True)
      return None
    final_path = self.partial_path.with_suffix("")
    os.replace(self.partial_path, final_path)
    return final_path
