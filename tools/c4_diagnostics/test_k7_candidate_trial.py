# K7 레이더 후보의 거절 분류와 독립 복원 확인을 검증한다.
import unittest
from unittest.mock import patch

from tools.c4_diagnostics.k7_candidate_trial import CANDIDATE, ORIGINAL, observe_can, run_trial, run_security_survey


class Rejection(Exception):
  def __init__(self, code):
    super().__init__(f'NRC 0x{code:02x}')
    self.error_code = code


class FakeClient:
  def __init__(self):
    self.config = ORIGINAL
    self.session = 1
    self.reject_candidate = None
    self.reject_restore = False
    self.writes = []
    self.security_requests = []
    self.reject_security = None

  def read_data_by_identifier(self, did):
    if did == 0xf100:
      return b'YG__ SCC FHCUP      1.00 1.02 99110-F6000'
    return self.config

  def diagnostic_session_control(self, session):
    self.session = session

  def write_data_by_identifier(self, _did, value):
    self.writes.append(value)
    if value == CANDIDATE and self.reject_candidate:
      raise Rejection(self.reject_candidate)
    if value == ORIGINAL and self.reject_restore:
      raise Rejection(0x33)
    self.config = value

  def security_access(self, level):
    self.security_requests.append(level)
    rejection = self.reject_security.get(level) if isinstance(self.reject_security, dict) else self.reject_security
    if rejection:
      raise Rejection(rejection)
    return b'\x12\x34'


class TestK7CandidateTrial(unittest.TestCase):
  def test_security_survey_requests_three_seed_levels_and_restores_session(self):
    client = FakeClient()
    report = run_security_survey(client)
    self.assertEqual(client.security_requests, [0x01, 0x03, 0x05])
    self.assertEqual(client.writes, [])
    self.assertEqual(client.session, 1)
    self.assertEqual(report['status'], 'seed_accepted')
    self.assertEqual(report['seed_length'], 2)
    self.assertEqual([entry['level'] for entry in report['security_exchanges']], ['0x01', '0x03', '0x05'])
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertFalse(report['key_sent'])
    self.assertFalse(report['write_performed'])
    self.assertNotIn('1234', str(report))

  def test_security_survey_records_rejection_without_writing(self):
    client = FakeClient()
    client.reject_security = 0x31
    report = run_security_survey(client)
    self.assertEqual(report['status'], 'seed_rejected')
    self.assertEqual(report['errors'][0]['stage'], 'security_seed_01')
    self.assertEqual(report['errors'][0]['nrc'], '0x31')
    self.assertEqual(client.security_requests, [0x01, 0x03, 0x05])
    self.assertEqual([entry['level'] for entry in report['security_exchanges']], ['0x01', '0x03', '0x05'])
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [])

  def test_security_survey_falls_back_once_when_level_03_unsupported(self):
    client = FakeClient()
    client.reject_security = {0x03: 0x12}
    report = run_security_survey(client)
    self.assertEqual(client.security_requests, [0x01, 0x03, 0x05])
    self.assertEqual(report['security_level'], '0x05')
    self.assertEqual(report['status'], 'seed_accepted')
    self.assertEqual([entry['status'] for entry in report['security_exchanges']], ['accepted', 'rejected', 'accepted'])
    self.assertEqual(client.writes, [])

  def test_security_survey_stops_on_security_denial(self):
    client = FakeClient()
    client.reject_security = 0x33
    report = run_security_survey(client)
    self.assertEqual(client.security_requests, [0x01])
    self.assertEqual(report['status'], 'stopped_after_rejection')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [])

  def test_security_survey_stops_after_later_level_denial(self):
    client = FakeClient()
    client.reject_security = {0x03: 0x33}
    report = run_security_survey(client)
    self.assertEqual(client.security_requests, [0x01, 0x03])
    self.assertEqual([entry['status'] for entry in report['security_exchanges']], ['accepted', 'rejected'])
    self.assertEqual(report['status'], 'stopped_after_rejection')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [])

  def test_security_survey_records_dtc_and_can_before_after(self):
    client = FakeClient()
    dtcs = iter((b'\x11', b'\x11'))
    observations = iter(({'track_range_frames': count} for count in (0, 1, 2, 3, 4, 0)))
    report = run_security_survey(client, observe=lambda: next(observations), read_dtc=lambda: next(dtcs))
    self.assertFalse(report['dtc_changed'])
    self.assertEqual(report['dtc_before_hex'], '11')
    self.assertEqual(report['dtc_after_hex'], '11')
    self.assertEqual([report['can_observations'][key]['track_range_frames'] for key in
                      ('before', 'extended_session', 'after_seed_01', 'after_seed_03', 'after_seed_05', 'restored')],
                     [0, 1, 2, 3, 4, 0])
    self.assertEqual(client.writes, [])

  def test_security_survey_observes_each_supported_seed_request(self):
    client = FakeClient()
    client.reject_security = {0x03: 0x31}
    observations = iter(({'track_range_frames': count} for count in range(6)))
    report = run_security_survey(client, observe=lambda: next(observations))
    self.assertEqual(client.security_requests, [0x01, 0x03, 0x05])
    self.assertEqual([report['can_observations'][key]['track_range_frames'] for key in
                      ('before', 'extended_session', 'after_seed_01', 'after_seed_03', 'after_seed_05', 'restored')],
                     list(range(6)))
    self.assertEqual(client.writes, [])

  def test_security_survey_can_observation_error_keeps_restoration(self):
    client = FakeClient()
    observations = iter(({'track_range_frames': 0}, RuntimeError('CAN unavailable'),
                         {'track_range_frames': 0}, {'track_range_frames': 0},
                         {'track_range_frames': 0}, {'track_range_frames': 0}))

    def observe():
      result = next(observations)
      if isinstance(result, Exception):
        raise result
      return result

    report = run_security_survey(client, observe=observe)
    self.assertEqual(report['errors'][0]['stage'], 'can_extended_session')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.session, 1)
    self.assertEqual(client.writes, [])

  def test_security_survey_flags_changed_dtc_for_review(self):
    client = FakeClient()
    dtcs = iter((b'\x11', b'\x12'))
    report = run_security_survey(client, read_dtc=lambda: next(dtcs))
    self.assertTrue(report['dtc_changed'])
    self.assertEqual(report['status'], 'dtc_changed_review_required')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [])

  def test_security_survey_requires_original_configuration(self):
    client = FakeClient()
    client.config = CANDIDATE
    report = run_security_survey(client)
    self.assertEqual(report['status'], 'initial_config_mismatch')
    self.assertEqual(client.security_requests, [])
    self.assertEqual(client.writes, [])
    self.assertEqual(report['restore_status'], 'unverified')

  def test_can_observation_uses_panda_address_data_bus_order(self):
    class FakePanda:
      def can_recv(self):
        return [(0x500, b'\x01', 2), (0x501, b'\x00\x80' + b'\x00' * 6, 1), (0x420, b'\x00', 0)]

    with patch('tools.c4_diagnostics.k7_candidate_trial.time.monotonic', side_effect=[0.0, 0.0, 1.0]):
      result = observe_can(FakePanda())
    self.assertEqual(result['track_address_bus_counts'], {'2:500': 1, '1:501': 1})
    self.assertEqual(result['track_range_frames'], 2)
    self.assertEqual(result['scc11_frames'], 1)
    self.assertEqual(result['track_dlc_by_bus'], {'2:1': 1, '1:8': 1})
    self.assertEqual(result['legacy_state_by_bus'], {'1:4': 1})

  def test_accepted_candidate_is_restored(self):
    client = FakeClient()
    observations = iter(({'track_range_frames': 0}, {'track_range_frames': 2}, {'track_range_frames': 0}))
    report = run_trial(client, observe=lambda: next(observations))
    self.assertEqual(report['status'], 'candidate_accepted')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [CANDIDATE, ORIGINAL])
    self.assertEqual(client.config, ORIGINAL)
    self.assertEqual(client.session, 1)
    self.assertEqual([report['can_observations'][key]['track_range_frames'] for key in
                      ('before', 'candidate', 'restored')], [0, 2, 0])

  def test_out_of_range_does_not_prove_value_is_wrong(self):
    client = FakeClient()
    client.reject_candidate = 0x31
    report = run_trial(client)
    self.assertEqual(report['status'], 'write_rejected_ambiguous')
    self.assertEqual(report['errors'][0]['nrc'], '0x31')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.config, ORIGINAL)
    self.assertNotIn('candidate', report['can_observations'])

  def test_security_denial_does_not_reject_value(self):
    client = FakeClient()
    client.reject_candidate = 0x33
    report = run_trial(client)
    self.assertEqual(report['status'], 'prerequisite_blocked')
    self.assertEqual(report['blocked_by'], 'security_access_denied')
    self.assertEqual(report['restore_status'], 'confirmed')

  def test_observation_failure_after_write_still_restores(self):
    client = FakeClient()
    calls = iter(({'track_range_frames': 0}, RuntimeError('CAN read failed')))

    def observe():
      value = next(calls)
      if isinstance(value, Exception):
        raise value
      return value

    report = run_trial(client, observe=observe)
    self.assertEqual(client.writes, [CANDIDATE, ORIGINAL])
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(report['errors'][0]['stage'], 'candidate_can_observation')

  def test_restore_failure_is_unverified(self):
    client = FakeClient()
    client.reject_restore = True
    report = run_trial(client)
    self.assertEqual(report['restore_status'], 'unverified')
    self.assertFalse(report['final_config_verified'])
    self.assertEqual(client.config, CANDIDATE)

  def test_unknown_initial_config_is_never_overwritten(self):
    client = FakeClient()
    client.config = bytes.fromhex('0002000002')
    report = run_trial(client)
    self.assertEqual(report['status'], 'initial_config_mismatch')
    self.assertEqual(client.writes, [])

  def test_independent_restore_repairs_only_known_candidate(self):
    client = FakeClient()
    client.config = CANDIDATE
    report = run_trial(client, restore_only=True)
    self.assertEqual(report['status'], 'original_restored')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertEqual(client.writes, [ORIGINAL])

  def test_independent_restore_refuses_unknown_config(self):
    client = FakeClient()
    client.config = bytes.fromhex('0002000002')
    report = run_trial(client, restore_only=True)
    self.assertEqual(report['status'], 'unexpected_config')
    self.assertEqual(client.writes, [])
    self.assertEqual(report['restore_status'], 'unverified')


if __name__ == '__main__':
  unittest.main()
