"""Radar lead-selection runtime."""


def effective_radar_track_mode(
  brand: str,
  radar_unavailable: bool,
  configured_mode: int,
  classic_238: bool = False,
) -> int:
  """Keep radar-source options Hyundai-only and auto-select other cars."""
  if brand == "hyundai":
    if configured_mode == 5:
      # K7 0x238 objects are front-radar points without SCC; elsewhere 5 means 0.
      return 1 if classic_238 else 0
    return int(configured_mode)
  return -2 if radar_unavailable else 1
