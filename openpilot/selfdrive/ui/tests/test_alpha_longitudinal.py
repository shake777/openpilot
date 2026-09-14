# 알파 종방향 제어 스위치의 표시 조건을 검증하는 테스트
import unittest
from types import SimpleNamespace

from openpilot.selfdrive.ui.alpha_longitudinal import alpha_longitudinal_toggle_visible


class TestAlphaLongitudinalToggle(unittest.TestCase):
  def test_non_release_without_car_params_is_visible(self):
    self.assertTrue(alpha_longitudinal_toggle_visible(None, False))

  def test_vehicle_support_is_checked_when_available(self):
    self.assertTrue(alpha_longitudinal_toggle_visible(SimpleNamespace(alphaLongitudinalAvailable=True), False))
    self.assertFalse(alpha_longitudinal_toggle_visible(SimpleNamespace(alphaLongitudinalAvailable=False), False))

  def test_release_build_remains_hidden(self):
    self.assertFalse(alpha_longitudinal_toggle_visible(None, True))
    self.assertFalse(alpha_longitudinal_toggle_visible(SimpleNamespace(alphaLongitudinalAvailable=True), True))


if __name__ == "__main__":
  unittest.main()
