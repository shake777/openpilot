#!/usr/bin/env python3
"""Some Hyundai radars can be reconfigured to output (debug) radar points on bus 1.
Reconfiguration is done over UDS by reading/writing to 0x0142 using the Read/Write Data By Identifier
endpoints (0x22 & 0x2E). This script checks your radar firmware version against a list of known
firmware versions. If you want to try on a new radar make sure to note the default config value
in case it's different from the other radars and you need to revert the changes.

After changing the config the car should not show any faults when openpilot is not running.
These config changes are persistent across car reboots. You need to run this script again
to go back to the default values.

USE AT YOUR OWN RISK! Safety features, like AEB and FCW, might be affected by these changes."""

import sys
import argparse
import json
import re
import time
from pathlib import Path
from typing import NamedTuple
from subprocess import check_output, CalledProcessError

class ConfigValues(NamedTuple):
  default_config: bytes
  tracks_enabled: bytes

# If your radar supports changing data identifier 0x0142 as well make a PR to
# this file to add your firmware version. Make sure to post a drive as proof!
# NOTE: these firmware versions do not match what openpilot uses
#       because this script uses a different diagnostic session type
SUPPORTED_FW_VERSIONS = {
  # 2020 SONATA
  b"DN8_ SCC FHCUP      1.00 1.00 99110-L0000\x19\x08)\x15T    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  b"DN8_ SCC F-CUP      1.00 1.00 99110-L0000\x19\x08)\x15T    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2021 SONATA HYBRID
  b"DNhe SCC FHCUP      1.00 1.00 99110-L5000\x19\x04&\x13'    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  b"DNhe SCC FHCUP      1.00 1.02 99110-L5000 \x01#\x15#    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2020 PALISADE
  b"LX2_ SCC FHCUP      1.00 1.04 99110-S8100\x19\x05\x02\x16V    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2022 PALISADE
  b"LX2_ SCC FHCUP      1.00 1.00 99110-S8110!\x04\x05\x17\x01    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2020 SANTA FE
  b"TM__ SCC F-CUP      1.00 1.03 99110-S2000\x19\x050\x13'    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2020 GENESIS G70
  b'IK__ SCC F-CUP      1.00 1.02 96400-G9100\x18\x07\x06\x17\x12    ': ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2019 SANTA FE
  b"TM__ SCC F-CUP      1.00 1.00 99110-S1210\x19\x01%\x168    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  b"TM__ SCC F-CUP      1.00 1.02 99110-S2000\x18\x07\x08\x18W    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
  # 2021 K5 HEV
  b"DLhe SCC FHCUP      1.00 1.02 99110-L7000 \x01 \x102    ": ConfigValues(
    default_config=b"\x00\x00\x00\x01\x00\x00",
    tracks_enabled=b"\x00\x00\x00\x01\x00\x01"),
}

PART_NUMBER_PATTERN = re.compile(r'\b\d{5}-[A-Z0-9]{5}\b')
VERSION_PATTERN = re.compile(r'\b\d+\.\d+\b')
K7_EXPERIMENTAL_CONFIG = ConfigValues(
  default_config=b"\x00\x02\x00\x00\x00",
  tracks_enabled=b"\x00\x02\x00\x00\x01",
)
K7_BACKUP_PATH = Path("/data/k7-radar-0142-backup.json")


def is_k7_experimental_firmware(fw_version: bytes) -> bool:
  return b"YG__ SCC FHCUP" in fw_version and b"99110-F6000" in fw_version and b"1.00 1.02" in fw_version


def save_k7_backup(fw_version: bytes, current_config: bytes, path: Path = K7_BACKUP_PATH) -> None:
  record = {"firmware_hex": fw_version.hex(), "original_config_hex": current_config.hex()}
  if path.exists():
    existing = json.loads(path.read_text(encoding="utf-8"))
    if existing != record:
      raise ValueError(f"backup mismatch at {path}")
    return
  path.parent.mkdir(parents=True, exist_ok=True)
  temp_path = path.with_suffix(path.suffix + ".tmp")
  temp_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
  temp_path.replace(path)


def load_k7_backup(path: Path = K7_BACKUP_PATH) -> bytes:
  record = json.loads(path.read_text(encoding="utf-8"))
  return bytes.fromhex(record["original_config_hex"])


def read_radar_inventory(uds_client, bus: int) -> dict:
  from opendbc.car.uds import NegativeResponseError, MessageTimeoutError

  inventory = {
    'mode': 'inventory-only',
    'write_performed': False,
    'diagnostic_session_changed': False,
    'ecu': {'request_address': '0x7d0', 'response_address': '0x7d8', 'bus': bus},
    'f100': {'status': 'not_read'},
    'did_0142': {'status': 'not_read'},
    'known_activation_support': 'unknown',
    'firmware_exact_match': None,
    'complete': False,
  }
  for data_id, field in ((0xf100, 'f100'), (0x0142, 'did_0142')):
    try:
      data = uds_client.read_data_by_identifier(data_id)
    except (NegativeResponseError, MessageTimeoutError) as error:
      inventory[field] = {
        'status': 'error', 'did': f'0x{data_id:04x}',
        'error_type': type(error).__name__, 'message': str(error),
      }
      if isinstance(error, NegativeResponseError):
        inventory[field]['nrc'] = error.error_code
      else:
        inventory[field]['hint'] = 'Check vehicle ignition and diagnostic bus; timeout does not establish the cause.'
      return inventory
    inventory[field] = {'status': 'ok', 'raw_hex': data.hex(), 'bytes': len(data)}
    if data_id == 0xf100:
      readable = ''.join(chr(value) if 32 <= value < 127 else '.' for value in data)
      part_number = PART_NUMBER_PATTERN.search(readable)
      inventory[field].update({
        'ascii': readable,
        'part_number': part_number.group(0) if part_number else '',
        'version_fields': VERSION_PATTERN.findall(readable),
      })
      inventory['firmware_exact_match'] = data in SUPPORTED_FW_VERSIONS
      # 기본 세션 정보만으로 실제 차량의 트랙 활성화 지원을 확정하지 않는다.
  inventory['complete'] = True
  return inventory

if __name__ == "__main__":
  from opendbc.car.carlog import carlog
  from opendbc.car.uds import UdsClient, SESSION_TYPE, DATA_IDENTIFIER_TYPE, MessageTimeoutError, NegativeResponseError
  from opendbc.car.structs import CarParams
  from panda.python import Panda

  parser = argparse.ArgumentParser(description='configure radar to output points (or reset to default)')
  parser.add_argument('--default', action="store_true", default=False, help='reset to default configuration (default: false)')
  parser.add_argument('--read-only', action="store_true", default=False,
                      help='only read firmware and configuration; never write to the radar')
  parser.add_argument('--inventory-only', action="store_true", default=False,
                      help='read only F100 and 0142 in the default session; never change session or write')
  parser.add_argument('--k7-experimental', action="store_true", default=False,
                      help='explicitly test K7 99110-F6000 candidate; saves and verifies the original configuration')
  parser.add_argument('--k7-session-probe', action="store_true", default=False,
                      help='read-only probe of the standard extended diagnostic session on the exact K7 radar')
  parser.add_argument('--backup-path', default=str(K7_BACKUP_PATH), help=argparse.SUPPRESS)
  parser.add_argument('--scan-config-dids', action="store_true", default=False,
                      help='with --read-only, scan manufacturer DIDs 0x0100-0x01ff after 0x0142 fails')
  parser.add_argument('--debug', action="store_true", default=False, help='enable debug output (default: false)')
  parser.add_argument('--bus', type=int, default=0, help='can bus to use (default: 0)')
  args = parser.parse_args()
  k7_backup_path = Path(args.backup_path)

  if args.scan_config_dids and not args.read_only:
    parser.error('--scan-config-dids requires --read-only')
  if args.inventory_only and (args.read_only or args.scan_config_dids or args.default or args.k7_session_probe):
    parser.error('--inventory-only cannot be combined with other diagnostic modes')
  if args.k7_experimental and (args.read_only or args.inventory_only or args.scan_config_dids or args.k7_session_probe):
    parser.error('--k7-experimental cannot be combined with read-only options')
  if args.k7_session_probe and (args.read_only or args.scan_config_dids or args.default):
    parser.error('--k7-session-probe is a standalone read-only mode')

  if args.debug:
    carlog.setLevel('DEBUG')

  try:
    check_output(["pidof", "pandad"])
    print("pandad is running, please kill openpilot before running this script! (aborted)")
    sys.exit(1)
  except CalledProcessError as e:
    if e.returncode != 1: # 1 == no process found (pandad not running)
      raise e

  confirm = input("power on the vehicle keeping the engine off (press start button twice) then type OK to continue: ").upper().strip()
  if confirm != "OK":
    print("\nyou didn't type 'OK! (aborted)")
    sys.exit(0)

  panda = Panda()
  panda.set_safety_mode(CarParams.SafetyModel.elm327)
  uds_client = UdsClient(panda, 0x7D0, bus=args.bus)

  if args.inventory_only:
    print("\n[STRICT READ-ONLY RADAR INVENTORY]")
    try:
      inventory = read_radar_inventory(uds_client, args.bus)
    finally:
      panda.close()
    print(json.dumps(inventory, ensure_ascii=False, sort_keys=True))
    print("inventory-only mode did not change diagnostic session and made no writes")
    sys.exit(0 if inventory['complete'] else 2)

  if args.k7_session_probe:
    print("\n[K7 READ-ONLY EXTENDED SESSION PROBE]")
    result = 2
    entered_extended_session = False
    try:
      fw_version = uds_client.read_data_by_identifier(0xf100)
      current_config = uds_client.read_data_by_identifier(0x0142)
      print(f"firmware: {fw_version!r}")
      print(f"default-session config: 0x{current_config.hex()}")
      if not is_k7_experimental_firmware(fw_version):
        print("K7 experimental firmware identity mismatch; session probe aborted")
      elif current_config != K7_EXPERIMENTAL_CONFIG.default_config:
        print("K7 config does not match the measured factory value; session probe aborted")
      else:
        print("[TRY STANDARD EXTENDED DIAGNOSTIC SESSION 0x03]")
        uds_client.diagnostic_session_control(SESSION_TYPE.EXTENDED_DIAGNOSTIC)
        entered_extended_session = True
        extended_config = uds_client.read_data_by_identifier(0x0142)
        print(f"extended-session config: 0x{extended_config.hex()}")
        print("K7 extended diagnostic session 0x03 is readable; no configuration write was attempted")
        result = 0
    except (MessageTimeoutError, NegativeResponseError) as error:
      print(f"K7 extended diagnostic session probe failed: {error}")
      print("session probe made no configuration write")
    finally:
      if entered_extended_session:
        try:
          uds_client.diagnostic_session_control(0x01)
          print("K7 diagnostic session returned to default 0x01")
        except (MessageTimeoutError, NegativeResponseError) as error:
          print(f"K7 default-session return failed: {error}; fully power off the vehicle before further testing")
          result = 3
      panda.close()
    sys.exit(result)

  if not args.read_only:
    print("\n[START DIAGNOSTIC SESSION]")
    # K7 99110-F6000 rejects Hyundai's legacy 0x07 session but exposes DID 0x0142 in standard extended session 0x03.
    session_type : SESSION_TYPE = SESSION_TYPE.EXTENDED_DIAGNOSTIC if args.k7_experimental else 0x07
    uds_client.diagnostic_session_control(session_type)
  else:
    print("\n[READ-ONLY DEFAULT SESSION]")

  print("[HARDWARE/SOFTWARE VERSION]")
  fw_version_data_id : DATA_IDENTIFIER_TYPE = 0xf100
  fw_version = uds_client.read_data_by_identifier(fw_version_data_id)
  print(fw_version)
  k7_experimental = args.k7_experimental and is_k7_experimental_firmware(fw_version)
  if args.k7_experimental and not k7_experimental:
    print("K7 experimental firmware identity mismatch! (aborted)")
    sys.exit(1)
  if fw_version not in SUPPORTED_FW_VERSIONS and not args.read_only and not k7_experimental:
    print("radar not supported! (aborted)")
    sys.exit(1)

  print("[GET CONFIGURATION]")
  config_data_id : DATA_IDENTIFIER_TYPE = 0x0142
  try:
    current_config = uds_client.read_data_by_identifier(config_data_id)
  except NegativeResponseError as default_session_error:
    if not args.read_only:
      raise

    print(f"default session config read failed: {default_session_error}")
    print("[TRY EXTENDED DIAGNOSTIC SESSION]")
    try:
      uds_client.diagnostic_session_control(SESSION_TYPE.EXTENDED_DIAGNOSTIC)
    except NegativeResponseError as extended_session_error:
      print(f"extended diagnostic session failed: {extended_session_error}")
      print("radar configuration could not be read; read-only mode made no changes")
      sys.exit(2)

    try:
      current_config = uds_client.read_data_by_identifier(config_data_id)
    except NegativeResponseError as extended_read_error:
      print(f"extended session config read failed: {extended_read_error}")
      if args.scan_config_dids:
        print("[SCAN READ-ONLY CONFIGURATION DIDS 0x0100-0x01ff]")
        found_dids = 0
        for data_id in range(0x0100, 0x0200):
          time.sleep(0.01)
          try:
            data = uds_client.read_data_by_identifier(data_id)
          except NegativeResponseError:
            continue
          found_dids += 1
          print(f"DID 0x{data_id:04x}: 0x{data.hex()} {data!r}")
        print(f"supported configuration DIDs found: {found_dids}")
      print("radar configuration could not be read; read-only mode made no changes")
      sys.exit(0 if args.scan_config_dids else 2)
  print(f"current config: 0x{current_config.hex()}")

  if args.read_only:
    support_status = "supported" if fw_version in SUPPORTED_FW_VERSIONS else "unknown"
    print(f"radar configuration is {support_status}; read-only mode made no changes")
    sys.exit(0)

  config_values = K7_EXPERIMENTAL_CONFIG if k7_experimental else SUPPORTED_FW_VERSIONS[fw_version]
  if k7_experimental and not args.default and current_config != config_values.default_config:
    print("\nK7 config does not match the measured factory value! (aborted)")
    sys.exit(1)
  if k7_experimental:
    try:
      if not args.default:
        save_k7_backup(fw_version, current_config, k7_backup_path)
      new_config = load_k7_backup(k7_backup_path) if args.default and k7_backup_path.exists() else config_values.default_config if args.default else config_values.tracks_enabled
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
      print(f"K7 backup validation failed: {error} (aborted)")
      sys.exit(1)
  else:
    new_config = config_values.default_config if args.default else config_values.tracks_enabled
  if current_config != new_config:
    if not args.default and current_config != config_values.default_config:
      print("\ncurrent config does not match expected default! (aborted)")
      sys.exit(1)
    print("[CHANGE CONFIGURATION]")
    print(f"new config:     0x{new_config.hex()}")
    if k7_experimental:
      try:
        uds_client.write_data_by_identifier(config_data_id, new_config)
        verified_config = uds_client.read_data_by_identifier(config_data_id)
        if verified_config == new_config:
          print(f"K7 readback verified: 0x{verified_config.hex()}")
        else:
          print(f"K7 readback mismatch: 0x{verified_config.hex()}; restoring original configuration")
          raise ValueError("readback mismatch")
      except (MessageTimeoutError, NegativeResponseError, ValueError) as error:
        print(f"K7 activation verification failed: {error}; restoring original configuration")
        original_config = load_k7_backup(k7_backup_path)
        try:
          uds_client.write_data_by_identifier(config_data_id, original_config)
          restored_config = uds_client.read_data_by_identifier(config_data_id)
        except (MessageTimeoutError, NegativeResponseError) as restore_error:
          print(f"K7 automatic restore failed: {restore_error}; do not start or drive the vehicle")
          sys.exit(3)
        if restored_config != original_config:
          print(f"K7 restore mismatch: 0x{restored_config.hex()}; do not start or drive the vehicle")
          sys.exit(3)
        print(f"K7 restore verified: 0x{restored_config.hex()}")
        sys.exit(2)
    else:
      uds_client.write_data_by_identifier(config_data_id, new_config)

    print("[DONE]")
    print("\nrestart your vehicle and ensure there are no faults")
    if not args.default:
      print("you can run this script again with --default to go back to the original (factory) settings")
  else:
    print("[DONE]")
    print("\ncurrent config is already the desired configuration")
    sys.exit(0)
