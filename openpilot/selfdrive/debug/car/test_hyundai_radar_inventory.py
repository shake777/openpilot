# K7 레이더 인벤토리가 지정된 두 DID만 읽고 결과를 구조화하는지 검증한다.
import unittest

from openpilot.selfdrive.debug.car.hyundai_enable_radar_points import read_radar_inventory


class FakeUdsClient:
  def __init__(self):
    self.requests = []

  def read_data_by_identifier(self, data_id):
    self.requests.append(data_id)
    return {
      0xf100: b'YG__ SCC F-CUP      1.00 1.02 99110-F6000         ',
      0x0142: b'\x00\x00\x00\x01\x00\x00',
    }[data_id]


class RadarInventoryTests(unittest.TestCase):
  def test_inventory_reads_only_f100_and_0142(self):
    client = FakeUdsClient()

    inventory = read_radar_inventory(client, bus=0)

    self.assertEqual(client.requests, [0xf100, 0x0142])
    self.assertIs(inventory['write_performed'], False)
    self.assertIs(inventory['diagnostic_session_changed'], False)
    self.assertEqual(inventory['ecu'], {'request_address': '0x7d0', 'response_address': '0x7d8', 'bus': 0})
    self.assertEqual(inventory['f100']['part_number'], '99110-F6000')
    self.assertEqual(inventory['f100']['version_fields'], ['1.00', '1.02'])
    self.assertEqual(inventory['did_0142'], {'raw_hex': '000000010000', 'bytes': 6})
    self.assertIs(inventory['known_activation_support'], False)


if __name__ == '__main__':
  unittest.main()
