# K7 0x238 종방향 후보의 기본 비활성·필터 경계를 검증하는 테스트
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).parents[1] / 'radarcan.py'
module = ast.parse(SOURCE.read_text(encoding='utf-8'))
function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'k7_classic_longitudinal_points')


class RadarPoint:
  pass


namespace = {'structs': SimpleNamespace(RadarData=SimpleNamespace(RadarPoint=RadarPoint))}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), namespace)
candidate_points = namespace['k7_classic_longitudinal_points']


def track(status=2, distance=12.0, lateral=0.5, age_ns=0):
  return SimpleNamespace(track_id=3, status=status, last_seen_ns=1_000_000_000 - age_ns,
                         kinematics=SimpleNamespace(d_rel=distance, y_rel=lateral, v_rel=-2.0,
                                                    a_rel=float('nan'), yv_rel=0.0, v_lead=8.0))


class TestK7ClassicLongitudinal(unittest.TestCase):
  def test_gate_is_k7_pe_only_and_off_without_explicit_opt_in(self):
    source = SOURCE.read_text(encoding='utf-8')
    self.assertIn("CP.carFingerprint == CAR.KIA_K7_PE", source)
    self.assertIn("os.environ.get('K7_CLASSIC_RADAR_LONGITUDINAL') == '1'", source)
    self.assertIn('and not result.points', source)

  def test_confirmed_fresh_track_can_be_converted(self):
    points = candidate_points([track()], 1_000_000_000)
    self.assertEqual(len(points), 1)
    self.assertEqual((points[0].trackId, points[0].dRel, points[0].vRel), (4003, 12.0, -2.0))
    self.assertEqual(points[0].radarSource, 'frontRadar')

  def test_unknown_or_stale_or_implausible_tracks_are_rejected(self):
    tracks = [track(status=1), track(status=6), track(age_ns=150_000_001),
              track(distance=2.9), track(distance=200.1), track(lateral=2.1)]
    self.assertEqual(candidate_points(tracks, 1_000_000_000), [])


if __name__ == '__main__':
  unittest.main()
