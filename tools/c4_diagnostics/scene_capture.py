# C4 레이더와 모델의 동기화 장면을 제한된 JSON Lines 파일로 기록하는 모듈
import json
import math
import os
from pathlib import Path


SCHEMA = "c4-scene-v1"
MAX_SCENE_BYTES = 1536 * 1024


def _number(value):
  try:
    result = float(value)
  except (TypeError, ValueError):
    return None
  return round(result, 5) if math.isfinite(result) else None


def _first(values):
  return _number(values[0]) if len(values) else None


def _take(values, limit: int):
  for index in range(min(len(values), limit)):
    yield values[index]


def _xy(value, limit: int = 33):
  return [[x, y] for x, y in zip((_number(item) for item in _take(value.x, limit)),
                                  (_number(item) for item in _take(value.y, limit)))
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


def _bool(obj, name):
  value = getattr(obj, name, None)
  return bool(value) if value is not None else None


def _text(value, limit: int = 40):
  return value[:limit] if isinstance(value, str) and value else None


def _int(owner, name):
  value = getattr(owner, name, None)
  return int(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _present(owner):
  meta = getattr(owner, "meta", None)
  return owner is not None and bool(getattr(meta, "present", False))


def scene_nav(carrot_man, car_state, carrot_navi=None) -> dict:
  """External navigation (vNavi via carrotMan) next to what the car's own CAN reports, so stock
  navigation/HUD CAN signals can be matched against known turn/camera distances."""
  nav = {
    "stock_limit": _int(car_state, "speedLimit"),
    "stock_limit_dist": _number(getattr(car_state, "speedLimitDistance", None)),
    "left_blinker": _bool(car_state, "leftBlinker"),
    "right_blinker": _bool(car_state, "rightBlinker"),
  }
  if carrot_man is not None:
    nav.update({
      "active": _int(carrot_man, "activeCarrot"),
      "road_limit": _int(carrot_man, "nRoadLimitSpeed"),
      "spd_type": _int(carrot_man, "xSpdType"),
      "spd_limit": _int(carrot_man, "xSpdLimit"),
      "spd_dist": _int(carrot_man, "xSpdDist"),
      "turn": _int(carrot_man, "xTurnInfo"),
      "turn_dist": _int(carrot_man, "xDistToTurn"),
      "tbt": _text(getattr(carrot_man, "szTBTMainText", None)),
      "sdi": _text(getattr(carrot_man, "szSdiDescr", None)),
      "desired_speed": _int(carrot_man, "desiredSpeed"),
      "desired_source": _text(getattr(carrot_man, "desiredSource", None), 16),
      "vehicle_navi_active": _bool(carrot_man, "vehicleNaviActive"),
      "vehicle_navi_speed": _int(carrot_man, "vehicleNaviSpeed"),
    })
  # Carrot Navi v2 (TCP 7714) publishes carrotNavi; while connected it replaces the legacy fields.
  if carrot_navi is not None and bool(getattr(carrot_navi, "connected", False)):
    guidance = getattr(carrot_navi, "guidanceCurrent", None)
    speed = getattr(carrot_navi, "speed", None)
    lane = getattr(carrot_navi, "laneCurrent", None)
    nav["source"] = "v2"
    if _present(guidance):
      nav.update({"turn": _int(guidance, "turnType"), "turn_dist": _int(guidance, "distanceM"),
                  "tbt": _text(getattr(guidance, "mainText", None))})
    else:
      nav.update({"turn": None, "turn_dist": None, "tbt": None})
    if _present(speed):
      nav.update({
        "road_limit": _int(speed, "roadLimitKph") if getattr(speed, "roadLimitValid", False) else None,
        "spd_type": _int(speed, "sdiType") if getattr(speed, "sdiPresent", False) else None,
        "spd_dist": _int(speed, "sdiDistanceM") if getattr(speed, "sdiPresent", False) else None,
        "spd_limit": _int(speed, "sdiSpeedLimitKph") if getattr(speed, "sdiPresent", False) else None,
        "section_active": _bool(speed, "sectionActive"),
        "section_limit": _int(speed, "sectionSpeedLimitKph") if getattr(speed, "sectionPresent", False) else None,
      })
    else:
      nav.update({"road_limit": None, "spd_type": None, "spd_dist": None, "spd_limit": None})
    nav["road_category"] = _int(lane, "roadCategory") if _present(lane) else None
  return nav


def build_scene_frame(mono_time: int, car_state, model, live_tracks, radar_state, car_control,
                      longitudinal_plan=None, camera=None, settings=None, nav=None) -> dict:
  lane_lines = [_xy(line) for line in _take(model.laneLines, 4)]
  model_leads = [{
    "probability": _number(lead.prob),
    "x": _first(lead.x),
    "y": _first(lead.y),
    "v": _first(lead.v),
    "a": _first(lead.a),
  } for lead in _take(model.leadsV3, 3)]
  return {
    "t": int(mono_time),
    "v_ego": _number(car_state.vEgo),
    # Set speed (km/h); lets the server tell a long gap caused by the cruise speed cap apart.
    "v_cruise": _number(getattr(car_state, "vCruise", None)),
    "steering_angle_deg": _number(car_state.steeringAngleDeg),
    "points": [_radar_point(point) for point in _take(live_tracks.points, 64)],
    "path": _xy(model.position),
    "lane_lines": lane_lines,
    "lane_probs": [_number(value) for value in _take(model.laneLineProbs, 4)],
    "model_leads": model_leads,
    "lead_one": _lead(radar_state.leadOne),
    "lead_two": _lead(radar_state.leadTwo),
    "long_active": bool(car_control.longActive),
    "enabled": bool(car_control.enabled),
    "target_accel": _number(car_control.actuators.accel),
    # Graph/review fields (2026-09-30): measured acceleration, driver pedals,
    # stock blind-spot warnings and the planner's following target.
    "a_ego": _number(getattr(car_state, "aEgo", None)),
    "gas_pressed": _bool(car_state, "gasPressed"),
    "brake_pressed": _bool(car_state, "brakePressed"),
    "standstill": _bool(car_state, "standstill"),
    "steering_pressed": _bool(car_state, "steeringPressed"),
    "left_blindspot": _bool(car_state, "leftBlindspot"),
    "right_blindspot": _bool(car_state, "rightBlindspot"),
    "t_follow": _number(getattr(longitudinal_plan, "tFollow", None)) if longitudinal_plan is not None else None,
    "desired_distance": (_number(getattr(longitudinal_plan, "desiredDistance", None))
                         if longitudinal_plan is not None else None),
    # Road-camera projection for the qcamera overlay: calib = [roll, pitch, yaw,
    # height] (liveCalibration), cam = [width, height, focal] of the full-size
    # road camera. The server scales to the recorded video size.
    "lat_active": _bool(car_control, "latActive"),
    # Lateral settings for the centering review; sent on a subset of frames.
    **({"settings": {key: _number(value) for key, value in settings.items()}} if settings else {}),
    "calib": [_number(value) for value in camera["calib"]] if camera and camera.get("calib") else None,
    "cam": [_number(value) for value in camera["cam"]] if camera and camera.get("cam") else None,
    **({"nav": nav} if nav else {}),
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
