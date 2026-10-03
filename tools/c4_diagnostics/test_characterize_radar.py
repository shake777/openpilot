# K7 제한 읽기 진단이 금지된 요청 없이 결과·오류·설정 불변을 기록하는지 검증한다.
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.c4_diagnostics.characterize_radar import characterize, CONFIG_DIDS, IDENTITY_DIDS


class NegativeResponseError(Exception):
  def __init__(self, code):
    super().__init__(f'NRC {code:02x}')
    self.error_code = code


class FakeRadar:
  # No session/security/write/reset/DTC-clear methods: accidental calls fail tests.
  def __init__(self):
    self.calls = []
    self.responses = {0xf100: b'YG__ SCC FHCUP 1.00 1.02 99110-F6000',
                      0x0142: bytes.fromhex('0002000000'), 0xf186: NegativeResponseError(0x31)}
    self.dtc = [b'\xff', b'\xff']
    self.config_reads = 0
    self.final = None

  def read_data_by_identifier(self, did):
    self.calls.append(('read', did))
    if did == 0x0142:
      self.config_reads += 1
      if self.config_reads == 3 and self.final is not None:
        if isinstance(self.final, Exception):
          raise self.final
        return self.final
    result = self.responses.get(did, b'\0')
    if isinstance(result, Exception):
      raise result
    return result

  def read_dtc_information(self, kind, mask):
    self.calls.append(('dtc', kind, mask))
    result = self.dtc.pop(0)
    if isinstance(result, Exception):
      raise result
    return result


class TestCharacterizeRadar(unittest.TestCase):
  def setUp(self):
    self.client = FakeRadar()
    module = SimpleNamespace(DTC_REPORT_TYPE=SimpleNamespace(DTC_BY_STATUS_MASK=2),
                             DTC_STATUS_MASK_TYPE=SimpleNamespace(ALL=255),
                             MessageTimeoutError=TimeoutError, NegativeResponseError=NegativeResponseError)
    self.modules = patch.dict('sys.modules', {'opendbc.car.uds': module})
    self.modules.start()
    self.addCleanup(self.modules.stop)
    sleep = patch('tools.c4_diagnostics.characterize_radar.time.sleep')
    sleep.start()
    self.addCleanup(sleep.stop)

  def test_bounded_reads_without_session_or_security(self):
    report, code = characterize(self.client)
    self.assertEqual(code, 2)  # F186 unsupported, not security denied.
    self.assertTrue(report['final_config_verified'])
    self.assertFalse(report['dtc_changed'])
    self.assertEqual(report['restore_status'], 'not_needed')
    self.assertFalse(report['seed_requested'])
    self.assertFalse(report['write_performed'])
    self.assertEqual(report['errors'][0]['nrc'], '0x31')
    reads = [call[1] for call in self.client.calls if call[0] == 'read']
    self.assertEqual(reads, [0xf100, 0x0142, 0xf186, *IDENTITY_DIDS, *CONFIG_DIDS, 0x0142])
    self.assertNotIn(0xf18c, reads)
    self.assertEqual([call for call in self.client.calls if call[0] == 'dtc'], [('dtc', 2, 255)] * 2)

  def test_complete_success(self):
    self.client.responses[0xf186] = b'\x01'
    report, code = characterize(self.client)
    self.assertEqual(code, 0)
    self.assertEqual(report['status'], 'collected')

  def test_dtc_unsupported_does_not_prevent_other_reads(self):
    self.client.dtc = [NegativeResponseError(0x11), TimeoutError('no response')]
    report, code = characterize(self.client)
    self.assertEqual(code, 2)
    self.assertIsNone(report['dtc_changed'])
    self.assertTrue(report['final_config_verified'])
    self.assertIn('did_0148', report['reads'])

  def test_changed_dtc_requires_review(self):
    self.client.dtc[1] = b'\xff\x01\x02\x03\x04'
    report, code = characterize(self.client)
    self.assertEqual(code, 3)
    self.assertTrue(report['dtc_changed'])

  def test_final_config_timeout_and_change_require_review(self):
    for final in (TimeoutError('no response'), bytes.fromhex('0002000001')):
      with self.subTest(final=final):
        client = FakeRadar()
        client.final = final
        report, code = characterize(client)
        self.assertEqual(code, 3)
        self.assertFalse(report['final_config_verified'])

  def test_wrong_firmware_stops_before_optional_requests(self):
    self.client.responses[0xf100] = b'UNKNOWN'
    report, code = characterize(self.client)
    self.assertEqual(code, 5)
    self.assertEqual(self.client.calls, [('read', 0xf100)])
    self.assertEqual(report['status'], 'firmware_unverified')

  def test_unexpected_error_still_reads_final_configuration(self):
    self.client.responses[0x0140] = RuntimeError('unexpected transport failure')
    with self.assertRaises(RuntimeError):
      characterize(self.client)
    self.assertEqual(self.client.calls[-2], ('read', 0x0142))

  def test_session_comparison_reports_new_reads_security_denial_and_value_changes(self):
    self.client.diagnostic_session_control = Mock()
    original_read = self.client.read_data_by_identifier

    def read(did):
      last_call = self.client.diagnostic_session_control.call_args
      extended = last_call is not None and last_call.args == (3,)
      if did == 0x0140 and not extended:
        raise NegativeResponseError(0x31)
      if did == 0x0141 and extended:
        raise NegativeResponseError(0x33)
      if did == 0x0143 and extended:
        return b'\x01'
      return original_read(did)

    self.client.read_data_by_identifier = read
    report, code = characterize(self.client, compare_sessions=True)
    self.assertEqual(code, 2)
    self.assertEqual(report['session_comparison']['0x0140'], 'newly_readable')
    self.assertEqual(report['session_comparison']['0x0141'], 'security_denied')
    self.assertEqual(report['session_comparison']['0x0143'], 'value_changed')
    self.assertEqual(report['session_comparison']['0x0142'], 'unchanged')
    self.assertEqual([c.args for c in self.client.diagnostic_session_control.call_args_list], [(1,), (3,), (1,)])
    self.assertTrue(report['comparison_complete'])
    self.assertTrue(report['final_config_verified'])
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertFalse(report['seed_requested'])

  def test_session_entry_and_restore_failure_are_not_success(self):
    for errors, status, code in (([TimeoutError(), None], 'default_session_unverified', 5),
                                  ([None, NegativeResponseError(0x12), None], 'collected_with_errors', 2),
                                  ([None, None, TimeoutError()], 'restore_unverified', 3)):
      with self.subTest(status=status):
        client = FakeRadar()
        client.diagnostic_session_control = Mock(side_effect=errors)
        report, result = characterize(client, compare_sessions=True)
        self.assertEqual(result, code)
        self.assertEqual(report['status'], status)
        self.assertTrue(report['final_config_verified'])
        self.assertEqual(client.diagnostic_session_control.call_args.args, (1,))

  def test_extended_timeout_stops_scan_and_restores_default(self):
    self.client.diagnostic_session_control = Mock()
    original_read = self.client.read_data_by_identifier

    def read(did):
      if did == IDENTITY_DIDS[0] and self.client.diagnostic_session_control.call_args.args == (3,):
        raise TimeoutError('lost response')
      return original_read(did)

    self.client.read_data_by_identifier = read
    report, code = characterize(self.client, compare_sessions=True)
    self.assertEqual(code, 2)
    self.assertFalse(report['comparison_complete'])
    self.assertEqual(len(report['session_comparison']), 1)
    self.assertEqual(report['restore_status'], 'confirmed')

  def test_extended_configuration_mismatch_aborts_optional_reads(self):
    self.client.diagnostic_session_control = Mock()
    self.client.final = b'\x99'
    report, code = characterize(self.client, compare_sessions=True)
    self.assertEqual(code, 3)
    self.assertEqual(report['status'], 'extended_configuration_unverified')
    self.assertEqual(report['session_comparison'], {})
    self.assertTrue(report['final_config_verified'])

  def test_lost_baseline_response_is_not_newly_readable(self):
    self.client.diagnostic_session_control = Mock()
    original_read = self.client.read_data_by_identifier

    def read(did):
      if did == 0x0140 and self.client.diagnostic_session_control.call_args.args == (1,):
        raise TimeoutError('baseline lost')
      return original_read(did)

    self.client.read_data_by_identifier = read
    report, _ = characterize(self.client, compare_sessions=True)
    self.assertEqual(report['session_comparison']['0x0140'], 'baseline_unverified')

  def test_unverified_default_config_prevents_extended_session(self):
    self.client.diagnostic_session_control = Mock()
    original_read = self.client.read_data_by_identifier

    def read(did):
      if did == 0x0142 and self.client.config_reads == 1:
        self.client.config_reads += 1
        return b'\x99'
      return original_read(did)

    self.client.read_data_by_identifier = read
    report, code = characterize(self.client, compare_sessions=True)
    self.assertEqual(code, 3)
    self.assertEqual(report['status'], 'default_configuration_unverified')
    self.assertEqual([c.args for c in self.client.diagnostic_session_control.call_args_list], [(1,), (1,)])


if __name__ == '__main__':
  unittest.main()
