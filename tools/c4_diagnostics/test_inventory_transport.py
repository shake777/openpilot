# 실제 ISO-TP 구현에 가상 CAN 응답을 연결해 자동 진단의 다중 프레임 처리를 검증하는 테스트
import unittest
from collections import deque

from opendbc.car.can_definitions import CanData
from opendbc.car.isotp_parallel_query import IsoTpParallelQuery
from tools.c4_diagnostics.startup_inventory import collect_inventory


class TestInventoryTransport(unittest.TestCase):
  def test_actual_isotp_multiframe_firmware_and_configuration(self):
    incoming = deque()
    remaining = []
    sent = []
    firmware = b"TEST_ONLY_K7_FIRMWARE_0123456789"
    config = b"\x00\x00\x00\x01\x00\x00"

    def send(frames):
      nonlocal remaining
      for frame in frames:
        sent.append(frame)
        if frame.dat[0] == 0x30:
          incoming.append(remaining)
          remaining = []
          continue
        request = frame.dat[1:4]
        payload = b"\x62" + request[1:] + (firmware if request == b"\x22\xf1\x00" else config)
        first = bytes((0x10 | (len(payload) >> 8), len(payload) & 0xff)) + payload[:6]
        incoming.append([CanData(0x7d8, first, 0)])
        remaining = [CanData(0x7d8, (bytes((0x20 | (i & 0xf),)) + payload[offset:offset + 7]).ljust(8, b"\xaa"), 0)
                     for i, offset in enumerate(range(6, len(payload), 7), 1)]

    def receive(wait_for_one=False):
      return [incoming.popleft()] if incoming else []

    report = {}
    collect_inventory(receive, send, lambda: None, IsoTpParallelQuery, report)
    self.assertEqual(report["status"], "complete")
    self.assertEqual(report["f100"]["raw_hex"], firmware.hex())
    self.assertEqual(report["did_0142"]["raw_hex"], config.hex())
    self.assertEqual([f.dat[0] for f in sent], [3, 0x30, 3, 0x30])

  def test_actual_isotp_negative_response(self):
    incoming = deque()
    sent = []
    def send(frames):
      sent.extend(frames)
      incoming.append([CanData(0x7d8, b"\x03\x7f\x22\x31\xaa\xaa\xaa\xaa", 0)])
    report = {}
    collect_inventory(lambda wait_for_one=False: [incoming.popleft()] if incoming else [], send,
                      lambda: None, IsoTpParallelQuery, report)
    self.assertEqual(report["f100"], {"status": "rejected", "nrc": "0x31"})
    self.assertEqual(len(sent), 1)
    self.assertEqual(report["did_0142"]["status"], "not_read")


if __name__ == "__main__":
  unittest.main()
