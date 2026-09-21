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
    # 응답 형식 검사용 합성값이며 K7 실차 측정값이 아니다.
    self.responses = {
      0xf100: b'YG__ SCC F-CUP      1.00 1.02 99110-F6000         ',
      0x0142: b'\x00\x00\x00\x01\x00\x00',
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
    self.assertEqual(client.requests, [0xf100, 0x0142, 0x0142, 0x0142, 0x0142])
    client.security_access.assert_called_once_with(0x01)
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
    self.assertTrue(report['default_session_restored'])
    self.assertTrue(report['final_config_verified'])
    client.security_access.assert_called_once_with(0x01)
    client.write_data_by_identifier.assert_not_called()
    self.assertEqual(client.diagnostic_session_control.call_args_list, [
      unittest.mock.call(3), unittest.mock.call(1),
    ])

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

    self.assertEqual(code, 2)
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
