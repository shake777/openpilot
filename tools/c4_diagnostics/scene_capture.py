# C4 레이더와 모델의 동기화 장면을 제한된 JSON Lines 파일로 기록하는 모듈
import json
import math
import os
from pathlib import Path


SCHEMA = "c4-scene-v1"
MAX_SCENE_BYTES = 2 * 1024 * 1024


def _number(value):
  try:
    result = float(value)
  except (TypeError, ValueError):
    return None
  return round(result, 5) if math.isfinite(result) else None


def _first(values):
  return _number(values[0]) if len(values) else None


def _xy(value, limit: int = 33):
  return [[x, y] for x, y in zip((_number(item) for item in value.x[:limit]),
                                  (_number(item) for item in value.y[:limit]))
          if x is not None and y is not None]


def _radar_point(point):
  return {
    "track_id": int(point.trackId),
    "d_rel": _number(point.dRel),
    "y_rel": _number(point.yRel),
    "v_rel": _number(point.vRel),
    "v_lead": _number(point.vLead),
    "a_rel": _number(point.aRel),
    "measured": bool(point.measured),
    "source": str(point.radarSource),
    "track_state": int(point.trackState),
  }


def _lead(lead):
  return {
    "status": bool(lead.status),
    "radar": bool(lead.radar),
    "track_id": int(lead.radarTrackId),
    "d_rel": _number(lead.dRel),
    "y_rel": _number(lead.yRel),
    "v_rel": _number(lead.vRel),
    "v_lead": _number(lead.vLead),
    "d_path": _number(lead.dPath),
    "model_prob": _number(lead.modelProb),
  }


def build_scene_frame(mono_time: int, car_state, model, live_tracks, radar_state, car_control) -> dict:
  lane_lines = [_xy(line) for line in model.laneLines[:4]]
  model_leads = [{
    "probability": _number(lead.prob),
    "x": _first(lead.x),
    "y": _first(lead.y),
    "v": _first(lead.v),
    "a": _first(lead.a),
  } for lead in model.leadsV3[:3]]
  return {
    "t": int(mono_time),
    "v_ego": _number(car_state.vEgo),
    "steering_angle_deg": _number(car_state.steeringAngleDeg),
    "points": [_radar_point(point) for point in live_tracks.points[:64]],
    "path": _xy(model.position),
    "lane_lines": lane_lines,
    "lane_probs": [_number(value) for value in model.laneLineProbs[:4]],
    "model_leads": model_leads,
    "lead_one": _lead(radar_state.leadOne),
    "lead_two": _lead(radar_state.leadTwo),
    "long_active": bool(car_control.longActive),
    "enabled": bool(car_control.enabled),
    "target_accel": _number(car_control.actuators.accel),
  }


class SceneCaptureWriter:
  def __init__(self, spool_dir: Path, capture_name: str, started_at: float):
    self.started_at = started_at
    self.partial_path = spool_dir / f"{capture_name}.c4scene.partial"
    self.stream = self.partial_path.open("wb")
    header = json.dumps({"schema": SCHEMA}, separators=(",", ":")).encode() + b"\n"
    self.stream.write(header)
    self.size = len(header)
    self.frames = 0
    self.full = False

  def append(self, frame: dict) -> bool:
    encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if self.size + len(encoded) > MAX_SCENE_BYTES:
      self.full = True
      return False
    self.stream.write(encoded)
    self.size += len(encoded)
    self.frames += 1
    return True

  def should_rotate(self, now: float, max_age_seconds: float = 60.0) -> bool:
    return self.full or now - self.started_at >= max_age_seconds

  def finalize(self) -> Path | None:
    if self.stream.closed:
      return None
    self.stream.flush()
    os.fsync(self.stream.fileno())
    self.stream.close()
    if self.frames == 0:
      self.partial_path.unlink(missing_ok=True)
      return None
    final_path = self.partial_path.with_suffix("")
    os.replace(self.partial_path, final_path)
    return final_path

  def discard(self) -> None:
    if not self.stream.closed:
      self.stream.close()
    self.partial_path.unlink(missing_ok=True)
