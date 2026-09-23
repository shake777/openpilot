# K7 레이더 후보 쓰기 후 원본 복원을 검증한다.
import unittest

from openpilot.selfdrive.debug.car.hyundai_enable_radar_points import write_k7_candidate_and_restore


ORIGINAL = bytes.fromhex('0002000000')
CANDIDATE = bytes.fromhex('0002000001')


class FakeClient:
  def __init__(self):
    self.value = ORIGINAL
    self.writes = []
    self.reject_candidate = False
    self.reject_restore = False
    self.ignore_candidate = False

  def write_data_by_identifier(self, data_id, value):
    self.writes.append((data_id, value))
    if value == CANDIDATE and self.reject_candidate:
      raise ValueError('write rejected')
    if value == ORIGINAL and self.reject_restore:
      raise ValueError('restore rejected')
    if value == CANDIDATE and self.ignore_candidate:
      return
    self.value = value

  def read_data_by_identifier(self, _data_id):
    return self.value


class TestK7TrialRestore(unittest.TestCase):
  def test_accepted_candidate_is_restored(self):
    client = FakeClient()
    self.assertEqual(write_k7_candidate_and_restore(client, 0x0142, CANDIDATE, ORIGINAL), CANDIDATE)
    self.assertEqual(client.value, ORIGINAL)
    self.assertEqual(client.writes, [(0x0142, CANDIDATE), (0x0142, ORIGINAL)])

  def test_rejected_candidate_keeps_original(self):
    client = FakeClient()
    client.reject_candidate = True
    with self.assertRaisesRegex(ValueError, 'write rejected'):
      write_k7_candidate_and_restore(client, 0x0142, CANDIDATE, ORIGINAL)
    self.assertEqual(client.value, ORIGINAL)

  def test_restore_failure_is_not_success(self):
    client = FakeClient()
    client.reject_restore = True
    with self.assertRaisesRegex(ValueError, 'restore rejected'):
      write_k7_candidate_and_restore(client, 0x0142, CANDIDATE, ORIGINAL)
    self.assertEqual(client.value, CANDIDATE)

  def test_ignored_candidate_is_not_reported_as_accepted(self):
    client = FakeClient()
    client.ignore_candidate = True
    with self.assertRaisesRegex(ValueError, 'candidate readback mismatch'):
      write_k7_candidate_and_restore(client, 0x0142, CANDIDATE, ORIGINAL)
    self.assertEqual(client.value, ORIGINAL)


if __name__ == '__main__':
  unittest.main()
