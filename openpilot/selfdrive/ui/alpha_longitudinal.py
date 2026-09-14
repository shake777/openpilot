# 알파 종방향 제어 스위치의 표시 가능 여부를 판단하는 모듈


def alpha_longitudinal_toggle_visible(car_params, is_release: bool) -> bool:
  if is_release:
    return False
  return car_params is None or bool(car_params.alphaLongitudinalAvailable)
