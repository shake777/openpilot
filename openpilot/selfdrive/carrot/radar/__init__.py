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


def radar_lead_comfort_extras_enabled(brand: str, classic_238: bool = False) -> bool:
  """Whether radar leads get the radar-only following extras.

  These are the opening-gap headroom hold (extra TF up to 2.5 s), the
  LeadAccelResponse catch-up boost and the lead-deceleration preview. Vision
  leads never get them. K7 mode 5 follows its radar lead like a vision lead:
  radar distance/speed, but base TF and normal MPC costs. On the K7 the hold
  kept gaps long, then released into catch-up acceleration and braking.
  """
  return not (brand == "hyundai" and classic_238)
