# K7 레이더 인벤토리가 지정된 두 DID만 읽고 결과를 구조화하는지 검증한다.
import contextlib
import io
import json
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpilot.selfdrive.debug.car import hyundai_enable_radar_points as radar


class MessageTimeoutError(Exception):
  pass


class NegativeResponseError(Exception):
  def __init__(self, message, service_id, error_code):
    super().__init__(message)
    self.service_id = service_id
    self.error_code = error_code


class FakeUdsClient:
  def __init__(self):
    self.requests = []
    self.tester_present = Mock()
    # 응답 형식 검사용 합성값이며 K7 실차 측정값이 아니다.
    self.responses = {
      0xf100: b'YG__ SCC F-CUP      1.00 1.02 99110-F6000         ',
      0x0142: b'\x00\x00\x00\x01\x00\x00',
      0xf186: b'\x03',
    }

  def read_data_by_identifier(self, data_id):
    self.requests.append(data_id)
    response = self.responses[data_id]
    if isinstance(response, Exception):
      raise response
    return response


class RadarInventoryTests(unittest.TestCase):
  def setUp(self):
    self.uds_module = SimpleNamespace(NegativeResponseError=NegativeResponseError, MessageTimeoutError=MessageTimeoutError)
    self.modules = patch.dict('sys.modules', {'opendbc.car.uds': self.uds_module})
    self.modules.start()
    self.addCleanup(self.modules.stop)

  def run_cli(self, client, *args):
    panda = Mock()
    self.uds_module.UdsClient = Mock(return_value=client)
    self.uds_module.SESSION_TYPE = SimpleNamespace(EXTENDED_DIAGNOSTIC=3)
    self.uds_module.DATA_IDENTIFIER_TYPE = int
    modules = {
      'opendbc.car.carlog': SimpleNamespace(carlog=Mock()),
      'opendbc.car.structs': SimpleNamespace(CarParams=SimpleNamespace(SafetyModel=SimpleNamespace(elm327=3))),
      'panda.python': SimpleNamespace(Panda=Mock(return_value=panda)),
    }
    output = io.StringIO()
    with patch.dict('sys.modules', modules), patch('sys.argv', [radar.__file__, *args]), \
         patch('builtins.input', return_value='OK'), \
         patch('subprocess.check_output', side_effect=subprocess.CalledProcessError(1, 'pidof')), \
         contextlib.redirect_stdout(output):
      try:
        runpy.run_path(radar.__file__, run_name='__main__')
      except SystemExit as stopped:
        code = stopped.code
      else:
        code = 0
    return code, output.getvalue(), panda

  def test_characterization_cli_saves_read_only_report_and_closes_panda(self):
    from tools.c4_diagnostics.characterize_radar import CONFIG_DIDS, IDENTITY_DIDS

    client = FakeUdsClient()
    client.responses.update({did: b'\0' for did in CONFIG_DIDS + IDENTITY_DIDS})
    client.responses.update({0xf100: b'YG__ SCC FHCUP 1.00 1.02 99110-F6000',
                             0x0142: bytes.fromhex('0002000000'),
                             0xf186: NegativeResponseError('unsupported', 0x22, 0x31)})
    client.read_dtc_information = Mock(return_value=b'\xff')
    self.uds_module.DTC_REPORT_TYPE = SimpleNamespace(DTC_BY_STATUS_MASK=2)
    self.uds_module.DTC_STATUS_MASK_TYPE = SimpleNamespace(ALL=255)
    with tempfile.TemporaryDirectory() as directory, patch('tools.c4_diagnostics.characterize_radar.time.sleep'):
      output = Path(directory) / 'report.json'
      code, _, panda = self.run_cli(client, '--k7-characterize', '--probe-output', str(output))
      report = json.loads(output.read_text())
    self.assertEqual(code, 2)
    self.assertEqual(report['mode'], 'k7-characterize-no-session-change')
    self.assertTrue(report['final_config_verified'])
    self.assertFalse(report['seed_requested'])
    panda.close.assert_called_once()

  def test_characterization_cli_rejects_write_mode_combination(self):
    code, _, panda = self.run_cli(FakeUdsClient(), '--k7-characterize', '--k7-experimental')
    self.assertEqual(code, 2)
    panda.set_safety_mode.assert_not_called()

  def test_session_comparison_requires_characterization(self):
    code, _, panda = self.run_cli(FakeUdsClient(), '--compare-sessions', '--k7-security-probe')
    self.assertEqual(code, 2)
    panda.set_safety_mode.assert_not_called()

  def test_session_comparison_cli_forwards_mode_and_saves_report(self):
    report = {'mode': 'k7-characterize-session-comparison', 'session_comparison': {'0x0140': 'newly_readable'}}
    with tempfile.TemporaryDirectory() as directory, \
         patch('tools.c4_diagnostics.characterize_radar.characterize', return_value=(report, 2)) as characterize:
      output = Path(directory) / 'report.json'
      code, _, panda = self.run_cli(FakeUdsClient(), '--k7-characterize', '--compare-sessions', '--probe-output', str(output))
      self.assertEqual(code, 2)
      self.assertEqual(json.loads(output.read_text()), report)
      self.assertTrue(characterize.call_args.kwargs['compare_sessions'])
    panda.close.assert_called_once()

  def test_inventory_reads_only_f100_and_0142(self):
    client = FakeUdsClient()

    inventory = radar.read_radar_inventory(client, bus=0)

    self.assertEqual(client.requests, [0xf100, 0x0142])
    self.assertIs(inventory['write_performed'], False)
    self.assertIs(inventory['diagnostic_session_changed'], False)
    self.assertEqual(inventory['ecu'], {'request_address': '0x7d0', 'response_address': '0x7d8', 'bus': 0})
    self.assertEqual(inventory['f100']['part_number'], '99110-F6000')
    self.assertEqual(inventory['f100']['version_fields'], ['1.00', '1.02'])
    self.assertEqual(inventory['did_0142'], {'status': 'ok', 'raw_hex': '000000010000', 'bytes': 6})
    self.assertEqual(inventory['known_activation_support'], 'unknown')
    self.assertIs(inventory['firmware_exact_match'], False)
    self.assertTrue(inventory['complete'])

  def test_cli_preserves_partial_result_for_each_read_failure(self):
    for data_id in (0xf100, 0x0142):
      for error in (MessageTimeoutError('timeout waiting for response'), NegativeResponseError('denied', 0x22, 0x31)):
        with self.subTest(did=data_id, error=type(error).__name__):
          client = FakeUdsClient()
          client.responses[data_id] = error
          code, output, panda = self.run_cli(client, '--inventory-only')
          inventory = json.loads(next(line for line in output.splitlines() if line.startswith('{')))
          self.assertEqual(code, 2)
          self.assertFalse(inventory['complete'])
          field = 'f100' if data_id == 0xf100 else 'did_0142'
          self.assertEqual(inventory[field]['error_type'], type(error).__name__)
          self.assertEqual(inventory[field]['did'], f'0x{data_id:04x}')
          if isinstance(error, NegativeResponseError):
            self.assertEqual(inventory[field]['nrc'], 0x31)
          if data_id == 0x0142:
            self.assertEqual(inventory['f100']['raw_hex'], client.responses[0xf100].hex())
            self.assertEqual(client.requests, [0xf100, 0x0142])
          else:
            self.assertEqual(inventory['did_0142']['status'], 'not_read')
            self.assertEqual(client.requests, [0xf100])
          panda.close.assert_called_once_with()

  def test_exact_match_does_not_authorize_activation(self):
    client = FakeUdsClient()
    client.responses[0xf100] = next(iter(radar.SUPPORTED_FW_VERSIONS))
    inventory = radar.read_radar_inventory(client, 0)
    self.assertTrue(inventory['firmware_exact_match'])
    self.assertEqual(inventory['known_activation_support'], 'unknown')

  def test_inventory_cli_success_exits_before_session_or_write(self):
    client = FakeUdsClient()
    code, output, panda = self.run_cli(client, '--inventory-only')
    self.assertEqual(code, 0)
    self.assertEqual(client.requests, [0xf100, 0x0142])
    self.assertIn('"complete": true', output)
    panda.close.assert_called_once_with()

  def test_activation_rejects_unexpected_config_before_write(self):
    client = FakeUdsClient()
    client.responses[0xf100] = next(iter(radar.SUPPORTED_FW_VERSIONS))
    client.responses[0x0142] = b'\x99' * 6
    client.diagnostic_session_control = Mock()
    client.write_data_by_identifier = Mock()
    code, output, _ = self.run_cli(client)
    self.assertEqual(code, 1)
    self.assertIn('does not match expected default', output)
    client.write_data_by_identifier.assert_not_called()

  def test_known_configuration_paths(self):
    fw, config = next(iter(radar.SUPPORTED_FW_VERSIONS.items()))
    cases = (
      (config.default_config, (), config.tracks_enabled),
      (config.tracks_enabled, (), None),
      (config.tracks_enabled, ('--default',), config.default_config),
    )
    for current, args, expected in cases:
      with self.subTest(current=current, args=args):
        client = FakeUdsClient()
        client.responses.update({0xf100: fw, 0x0142: current})
        client.diagnostic_session_control = Mock()
        client.write_data_by_identifier = Mock()
        code, _, _ = self.run_cli(client, *args)
        self.assertEqual(code, 0)
        if expected is None:
          client.write_data_by_identifier.assert_not_called()
        else:
          client.write_data_by_identifier.assert_called_once_with(0x0142, expected)

  def test_k7_experimental_candidate_is_exactly_guarded_and_verified(self):
    client = FakeUdsClient()
    fw = b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         '
    client.responses.update({0xf100: fw, 0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config})
    client.diagnostic_session_control = Mock()

    def write_config(data_id, value):
      self.assertEqual(data_id, 0x0142)
      client.responses[data_id] = value

    client.write_data_by_identifier = Mock(side_effect=write_config)
    with tempfile.TemporaryDirectory() as temp_dir:
      backup_path = Path(temp_dir) / 'k7-backup.json'
      code, output, _ = self.run_cli(client, '--k7-experimental', '--backup-path', str(backup_path))
      backup = json.loads(backup_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 0)
    self.assertIn('K7 readback verified: 0x0002000001', output)
    self.assertEqual(backup['original_config_hex'], '0002000000')
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3),
      unittest.mock.call(1),
    ])
    client.write_data_by_identifier.assert_called_once_with(0x0142, radar.K7_EXPERIMENTAL_CONFIG.tracks_enabled)

  def test_k7_extended_session_probe_is_read_only_and_returns_to_default(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
    })
    client.diagnostic_session_control = Mock()
    client.write_data_by_identifier = Mock()

    code, output, panda = self.run_cli(client, '--k7-session-probe')

    self.assertEqual(code, 0)
    self.assertIn('extended diagnostic session 0x03 is readable', output)
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3),
      unittest.mock.call(1),
    ])
    client.write_data_by_identifier.assert_not_called()
    panda.close.assert_called_once_with()

  def test_k7_security_probe_requests_one_seed_without_key_or_write(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
    })
    client.diagnostic_session_control = Mock()
    client.security_access = Mock(return_value=b'\x12\x34\x56\x78')
    client.write_data_by_identifier = Mock()

    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, output, panda = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 0)
    self.assertIn('security seed request accepted (4 bytes); no key was sent', output)
    self.assertEqual(report['status'], 'seed_accepted')
    self.assertEqual(report['seed_length'], 4)
    self.assertTrue(report['default_session_restored'])
    self.assertTrue(report['final_config_verified'])
    self.assertEqual(report['post_restore_config_hex'], '0002000000')
    self.assertNotIn('12345678', report_path.name)
    self.assertNotIn('12345678', json.dumps(report))
    self.assertEqual(client.requests, [0xf100, 0x0142, 0x0142, 0xf186, 0x0142, 0x0142])
    client.security_access.assert_called_once_with(0x01)
    client.tester_present.assert_called_once_with()
    client.write_data_by_identifier.assert_not_called()
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3), unittest.mock.call(1),
    ])
    panda.close.assert_called_once_with()

  def test_k7_security_probe_rejection_still_verifies_factory_value(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
    })
    client.diagnostic_session_control = Mock()
    client.security_access = Mock(side_effect=NegativeResponseError('sub-function not supported', 0x27, 0x12))
    client.write_data_by_identifier = Mock()

    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, output, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 2)
    self.assertIn('security seed request rejected', output)
    self.assertIn('post-probe config: 0x0002000000', output)
    self.assertEqual(report['status'], 'seed_rejected')
    self.assertEqual(report['post_config_hex'], '0002000000')
    self.assertEqual(report['seed_nrc'], '0x12')
    self.assertEqual(report['active_session_before_seed_hex'], '03')
    self.assertTrue(report['default_session_restored'])
    self.assertTrue(report['final_config_verified'])
    client.security_access.assert_called_once_with(0x01)
    client.write_data_by_identifier.assert_not_called()
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3), unittest.mock.call(1),
    ])

  def test_k7_security_probe_keepalive_failure_skips_seed(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
    })
    client.diagnostic_session_control = Mock()
    client.tester_present.side_effect = NegativeResponseError('conditions not correct', 0x3e, 0x22)
    client.security_access = Mock()
    client.write_data_by_identifier = Mock()

    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 4)
    self.assertEqual(report['status'], 'session_unverified')
    self.assertEqual(report['errors'][-1]['stage'], 'session_keepalive')
    self.assertEqual(report['errors'][-1]['nrc'], '0x22')
    self.assertTrue(report['final_config_verified'])
    client.security_access.assert_not_called()
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_skips_seed_without_confirmed_session(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
                             0xf186: b'\x01'})
    client.diagnostic_session_control = Mock()
    client.security_access = Mock()
    client.write_data_by_identifier = Mock()
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 4)
    self.assertEqual(report['status'], 'session_not_active')
    self.assertEqual(report['restore_status'], 'confirmed')
    client.security_access.assert_not_called()
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_f186_unsupported_uses_positive_session_response(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
                             0xf186: NegativeResponseError('request out of range', 0x22, 0x31)})
    client.diagnostic_session_control = Mock()
    client.security_access = Mock(side_effect=NegativeResponseError('conditions not correct', 0x27, 0x22))
    client.write_data_by_identifier = Mock()
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 2)
    self.assertEqual(report['session_confirmation_source'], 'extended_response_and_0142')
    self.assertEqual(report['errors'][0]['nrc'], '0x31')
    self.assertEqual(report['seed_nrc'], '0x22')
    self.assertTrue(report['final_config_verified'])
    client.security_access.assert_called_once_with(0x01)
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_other_f186_error_still_skips_seed(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
                             0xf186: NegativeResponseError('conditions not correct', 0x22, 0x22)})
    client.diagnostic_session_control = Mock()
    client.security_access = Mock()
    client.write_data_by_identifier = Mock()
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 4)
    self.assertEqual(report['status'], 'session_unverified')
    self.assertIsNone(report['session_confirmation_source'])
    self.assertTrue(report['final_config_verified'])
    client.security_access.assert_not_called()
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_restores_after_session_entry_timeout(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config})
    client.diagnostic_session_control = Mock(side_effect=[MessageTimeoutError('no response'), None])
    client.security_access = Mock()
    client.write_data_by_identifier = Mock()
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 5)
    self.assertEqual(report['errors'][0]['stage'], 'session_enter')
    self.assertEqual(report['restore_status'], 'confirmed')
    self.assertTrue(report['final_config_verified'])
    self.assertEqual(client.diagnostic_session_control.call_args_list, [unittest.mock.call(3), unittest.mock.call(1)])
    client.security_access.assert_not_called()
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_restore_timeout_still_reads_configuration(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config})
    client.diagnostic_session_control = Mock(side_effect=[None, MessageTimeoutError('no response')])
    client.security_access = Mock(return_value=b'\x12\x34')
    client.write_data_by_identifier = Mock()
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 3)
    self.assertEqual(report['restore_status'], 'unconfirmed')
    self.assertEqual(report['post_restore_config_hex'], '0002000000')
    self.assertEqual(report['errors'][-1]['stage'], 'session_restore')
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_post_seed_read_failure_keeps_seed_result(self):
    client = FakeUdsClient()
    client.responses.update({0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000',
                             0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config})
    client.diagnostic_session_control = Mock()
    client.security_access = Mock(side_effect=NegativeResponseError('denied', 0x27, 0x22))
    client.write_data_by_identifier = Mock()
    original_read = client.read_data_by_identifier

    def fail_post_seed(data_id):
      value = original_read(data_id)
      if data_id == 0x0142 and client.requests.count(0x0142) == 3:
        raise MessageTimeoutError('post-seed read timed out')
      return value

    client.read_data_by_identifier = fail_post_seed
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, _, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))
    self.assertEqual(code, 2)
    self.assertEqual(report['status'], 'seed_rejected')
    self.assertEqual(report['seed_nrc'], '0x22')
    self.assertEqual(report['errors'][-1]['stage'], 'post_seed_read')
    self.assertTrue(report['final_config_verified'])
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_blocks_normal_resumption_if_final_config_differs(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: radar.K7_EXPERIMENTAL_CONFIG.default_config,
    })
    client.diagnostic_session_control = Mock()
    client.security_access = Mock(return_value=b'\x12\x34')
    client.write_data_by_identifier = Mock()
    original_read = client.read_data_by_identifier

    def changed_final_read(data_id):
      value = original_read(data_id)
      return b'\x00\x02\x00\x00\x01' if data_id == 0x0142 and client.requests.count(0x0142) == 4 else value

    client.read_data_by_identifier = changed_final_read
    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, output, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 3)
    self.assertIn('do not drive the vehicle', output)
    self.assertEqual(report['status'], 'restore_unverified')
    self.assertTrue(report['default_session_restored'])
    self.assertFalse(report['final_config_verified'])
    self.assertEqual(report['post_restore_config_hex'], '0002000001')
    client.write_data_by_identifier.assert_not_called()

  def test_k7_security_probe_rejects_other_firmware_before_session(self):
    client = FakeUdsClient()
    client.security_access = Mock()
    client.diagnostic_session_control = Mock()
    client.write_data_by_identifier = Mock()

    with tempfile.TemporaryDirectory() as temp_dir:
      report_path = Path(temp_dir) / 'k7-security-probe-test.json'
      code, output, _ = self.run_cli(client, '--k7-security-probe', '--probe-output', str(report_path))
      report = json.loads(report_path.read_text(encoding='utf-8'))

    self.assertEqual(code, 5)
    self.assertIn('firmware identity mismatch', output)
    self.assertEqual(report['status'], 'firmware_mismatch')
    client.security_access.assert_not_called()
    client.diagnostic_session_control.assert_not_called()
    client.write_data_by_identifier.assert_not_called()

  def test_k7_experimental_rejects_unexpected_default_without_write(self):
    client = FakeUdsClient()
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: b'\x00\x02\x00\x99\x00',
    })
    client.diagnostic_session_control = Mock()
    client.write_data_by_identifier = Mock()
    code, output, _ = self.run_cli(client, '--k7-experimental')
    self.assertEqual(code, 1)
    self.assertIn('does not match the measured factory value', output)
    client.write_data_by_identifier.assert_not_called()

  def test_k7_experimental_readback_mismatch_restores_original(self):
    client = FakeUdsClient()
    original = radar.K7_EXPERIMENTAL_CONFIG.default_config
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: original,
    })
    client.diagnostic_session_control = Mock()

    def write_config(data_id, value):
      if value == original:
        client.responses[data_id] = value
      else:
        client.responses[data_id] = b'\xff' * len(original)

    client.write_data_by_identifier = Mock(side_effect=write_config)
    with tempfile.TemporaryDirectory() as temp_dir:
      code, output, _ = self.run_cli(
        client, '--k7-experimental', '--backup-path', str(Path(temp_dir) / 'k7-backup.json'))

    self.assertEqual(code, 2)
    self.assertIn('K7 restore verified: 0x0002000000', output)
    self.assertEqual(client.write_data_by_identifier.call_args_list, [
      unittest.mock.call(0x0142, radar.K7_EXPERIMENTAL_CONFIG.tracks_enabled),
      unittest.mock.call(0x0142, original),
    ])

  def test_k7_rejected_write_verifies_original_without_rewriting_it(self):
    client = FakeUdsClient()
    original = radar.K7_EXPERIMENTAL_CONFIG.default_config
    client.responses.update({
      0xf100: b'YG__ SCC FHCUP      1.00 1.02 99110-F6000         ',
      0x0142: original,
    })
    client.diagnostic_session_control = Mock()
    client.write_data_by_identifier = Mock(side_effect=NegativeResponseError('denied', 0x2e, 0x31))
    with tempfile.TemporaryDirectory() as temp_dir:
      code, output, _ = self.run_cli(
        client, '--k7-experimental', '--backup-path', str(Path(temp_dir) / 'k7-backup.json'))

    self.assertEqual(code, 2)
    self.assertIn('K7 restore verified: 0x0002000000', output)
    client.write_data_by_identifier.assert_called_once_with(0x0142, radar.K7_EXPERIMENTAL_CONFIG.tracks_enabled)
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3),
      unittest.mock.call(1),
    ])


if __name__ == '__main__':
  unittest.main()
