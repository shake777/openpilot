# 보안 seed 원문 기록과 단일 요청 및 예외 보존을 검증한다.
import unittest
from unittest.mock import Mock

from tools.c4_diagnostics.seed_capture import capture_seed


class TestSeedCapture(unittest.TestCase):
  def test_response_preserved_with_single_request(self):
    client = Mock()
    client.security_access.return_value = b'\x00\x12'
    report = {'key_sent': False, 'write_performed': False}
    self.assertEqual(capture_seed(client, report), b'\x00\x12')
    self.assertEqual(report['seed_hex'], '0012')
    self.assertEqual(report['security_exchanges'][0]['request_hex'], '2701')
    self.assertEqual(report['security_exchanges'][0]['status'], 'accepted')
    self.assertFalse(report['key_sent'])
    self.assertFalse(report['write_performed'])
    self.assertEqual(client.mock_calls, [('security_access', (1,), {})])

  def test_errors_are_recorded_and_reraised_without_retry(self):
    for error in (TimeoutError('no response'), ValueError('denied')):
      if isinstance(error, ValueError):
        error.error_code = 0x33
      client = Mock()
      client.security_access.side_effect = error
      report = {}
      with self.assertRaises(type(error)) as raised:
        capture_seed(client, report)
      self.assertIs(raised.exception, error)
      self.assertNotIn('seed_hex', report)
      entry = report['security_exchanges'][0]
      self.assertEqual(entry['status'], 'error')
      self.assertGreaterEqual(entry['elapsed_s'], 0)
      if hasattr(error, 'error_code'):
        self.assertEqual(entry['nrc'], '0x33')
      client.security_access.assert_called_once_with(1)
