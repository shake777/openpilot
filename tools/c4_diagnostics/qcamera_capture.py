# C4 진단 묶음에 10분 주기 30초 qRoad H.264 영상을 저장하는 모듈
import json
import os
from pathlib import Path


VIDEO_DURATION_SECONDS = 30.0
# 256 kbps qRoad의 30초와 코덱 헤더 여유를 담되 전체 업로드는 5 MiB 아래로 유지한다.
MAX_QCAMERA_BYTES = 1152 * 1024
REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS = 10 * 60


def representative_video_due(now: float, next_video_time: float | None) -> bool:
  return next_video_time is not None and now >= next_video_time


def next_representative_video_time(now: float) -> float:
  return now + REPRESENTATIVE_CAPTURE_INTERVAL_SECONDS


def representative_video_rollover_due(now: float, next_video_time: float | None, video_active: bool) -> bool:
  return not video_active and representative_video_due(now, next_video_time)


class QCameraCaptureWriter:
  def __init__(self, spool_dir: Path, capture_name: str):
    self.partial_path = spool_dir / f"{capture_name}.qcamera.h264.partial"
    self.stream = self.partial_path.open("wb")
    self.size = 0
    self.frames = 0
    self.started_mono_ns = None
    self.last_mono_ns = None
    self.width = 0
    self.height = 0
    self.full = False

  def append(self, mono_time: int, encode_data) -> bool:
    header = bytes(encode_data.header)
    data = bytes(encode_data.data)
    if self.started_mono_ns is None:
      if not header or not data:
        return False
      self.started_mono_ns = int(mono_time)
      self.width = int(encode_data.width)
      self.height = int(encode_data.height)
    payload = (header if header else b"") + data
    if not payload or self.size + len(payload) > MAX_QCAMERA_BYTES:
      self.full = True
      return False
    self.stream.write(payload)
    self.size += len(payload)
    self.frames += 1
    self.last_mono_ns = int(mono_time)
    return True

  def complete(self) -> bool:
    return (self.full or self.started_mono_ns is not None and self.last_mono_ns is not None and
            self.last_mono_ns - self.started_mono_ns >= VIDEO_DURATION_SECONDS * 1e9)

  def finalize(self) -> tuple[Path | None, Path | None]:
    if self.stream.closed:
      return None, None
    self.stream.flush()
    os.fsync(self.stream.fileno())
    self.stream.close()
    if self.frames == 0:
      self.partial_path.unlink(missing_ok=True)
      return None, None
    video_path = self.partial_path.with_suffix("")
    os.replace(self.partial_path, video_path)
    duration = max(0.0, ((self.last_mono_ns or self.started_mono_ns) - self.started_mono_ns) / 1e9)
    metadata_path = video_path.with_suffix(".json")
    metadata_path.write_text(json.dumps({
      "schema": "c4-qcamera-video-v1", "codec": "h264", "container": "annex-b",
      "started_mono_ns": self.started_mono_ns, "duration_s": round(duration, 3),
      "frames": self.frames, "width": self.width, "height": self.height,
    }, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(video_path, 0o600)
    os.chmod(metadata_path, 0o600)
    return video_path, metadata_path
