# 실제 활성화 함수의 응답 판정과 진단 로그를 하드웨어 없이 검증한다.
import ast
import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class TestRadarActivation(unittest.TestCase):
  def setUp(self):
    # 플랫폼 의존 인터페이스 import 없이 저장소의 실제 함수 본문을 실행한다.
    tree = ast.parse(Path(__file__).parents[1].joinpath('interface.py').read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'enable_radar_tracks')
    self.namespace = {'HyundaiFlags': SimpleNamespace(CAMERA_SCC=SimpleNamespace(value=1))}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<radar activation>', 'exec'), self.namespace)

  def run_activation(self, reply=b'\x6e\x01\x42', session=b'\x50\x07', bus=0, reply_addr=0x7d0):
    calls = []
    packets = []
    recv = Mock(side_effect=lambda *args, **kwargs: packets)

    class FakeQuery:
      def __init__(self, send, receive, can_bus, addrs, request, response):
        self.receive = receive
        self.response = response[0]
        self.reply = session if not calls else reply
        calls.append({'bus': can_bus, 'addrs': addrs, 'request': request, 'response': response})

      def get_data(self, timeout, total_timeout):
        calls[-1].update(timeout=timeout, total_timeout=total_timeout)
        if isinstance(self.reply, Exception):
          raise self.reply
        if self.reply is None:
          return {}
        packets[:] = [[SimpleNamespace(address=reply_addr + 8, src=bus, dat=bytes([len(self.reply)]) + self.reply)]]
        self.receive(wait_for_one=True)
        return {(reply_addr, None): self.reply[len(self.response):]} if self.reply.startswith(self.response) else {}

    output = io.StringIO()
    with patch.dict('sys.modules', {'opendbc.car.isotp_parallel_query': SimpleNamespace(IsoTpParallelQuery=FakeQuery)}), \
         contextlib.redirect_stdout(output):
      result = self.namespace['enable_radar_tracks'](SimpleNamespace(flags=1 if bus == 2 else 0), recv, Mock())
    return result, calls, output.getvalue()

  def test_acknowledged_write_only(self):
    result, calls, output = self.run_activation()
    self.assertTrue(result)
    self.assertEqual(len(calls), 2)
    self.assertEqual(calls[0]['request'], [b'\x10\x07'])
    self.assertEqual(calls[1]['request'], [b'\x2e\x01\x42\x00\x00\x00\x01\x00\x01'])
    self.assertEqual(calls[1]['response'], [b'\x6e\x01\x42'])
    for call in calls:
      self.assertGreater(call['timeout'], 0)
      self.assertLessEqual(call['total_timeout'], 1.0)
    self.assertIn('data=036e0142', output)
    self.assertIn('track reception is not verified', output)

  def test_no_write_response_is_failure(self):
    result, calls, output = self.run_activation(reply=None)
    self.assertFalse(result)
    self.assertEqual(len(calls), 2)
    self.assertIn('stage=write failed', output)

  def test_session_failure_prevents_write(self):
    for reply in (None, b'\x7f\x10\x12', b'\x50\x03'):
      with self.subTest(reply=reply):
        result, calls, output = self.run_activation(session=reply)
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)
        self.assertIn('stage=session failed', output)

  def test_rejection_records_raw_response_and_nrc(self):
    result, _, output = self.run_activation(reply=b'\x7f\x2e\x31')
    self.assertFalse(result)
    self.assertIn('data=037f2e31', output)
    self.assertIn('service=0x2e NRC=0x31', output)

  def test_incorrect_service_or_did_is_failure(self):
    for reply in (b'\x68\x01\x42', b'\x6e\x01\x43', b'\x6e'):
      with self.subTest(reply=reply):
        self.assertFalse(self.run_activation(reply=reply)[0])

  def test_wrong_ecu_never_counts_as_success(self):
    result, calls, _ = self.run_activation(reply_addr=0x7d1)
    self.assertFalse(result)
    self.assertEqual(len(calls), 1)

  def test_camera_scc_bus_is_preserved(self):
    result, calls, _ = self.run_activation(bus=2)
    self.assertTrue(result)
    self.assertEqual([call['bus'] for call in calls], [2, 2])

  def test_exception_records_stage_and_type(self):
    result, _, output = self.run_activation(reply=TimeoutError('no ECU response'))
    self.assertFalse(result)
    self.assertIn('stage=write failed: TimeoutError: no ECU response', output)


if __name__ == '__main__':
  unittest.main()
