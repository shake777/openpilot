# K7 레이더 후보 하나를 정차 상태에서 시험하고 원본 설정을 재확인한다.
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from openpilot.selfdrive.debug.car.hyundai_enable_radar_points import is_k7_experimental_firmware


DATA_ID = 0x0142
ORIGINAL = bytes.fromhex('0002000000')
CANDIDATE = bytes.fromhex('0002000001')


def error_record(stage, error):
  record = {'stage': stage, 'error_type': type(error).__name__, 'message': str(error)}
  nrc = getattr(error, 'error_code', None)
  if isinstance(nrc, int):
    record['nrc'] = f'0x{nrc:02x}'
  return record


def run_trial(client, restore_only=False):
  report = {'schema': 'c4-k7-candidate-trial-v1', 'created_at': time.time(),
            'mode': 'restore_only' if restore_only else 'candidate_trial',
            'candidate_hex': CANDIDATE.hex(), 'original_hex': ORIGINAL.hex(),
            'status': 'not_started', 'write_attempted': False, 'write_acknowledged': False,
            'restore_write_attempted': False, 'final_config_verified': False,
            'default_session_restored': False, 'restore_status': 'not_attempted', 'errors': []}
  firmware_matched = False
  session_entered = False
  session_attempted = False
  may_restore = False
  stage = 'firmware_read'
  try:
    firmware = client.read_data_by_identifier(0xf100)
    report['firmware_hex'] = firmware.hex()
    firmware_matched = is_k7_experimental_firmware(firmware)
    if not firmware_matched:
      report['status'] = 'firmware_mismatch'
      return report

    stage = 'initial_config_read'
    initial = client.read_data_by_identifier(DATA_ID)
    report['initial_config_hex'] = initial.hex()
    if not restore_only and initial != ORIGINAL:
      report['status'] = 'initial_config_mismatch'
      return report
    if restore_only and initial == ORIGINAL:
      report['status'] = 'already_original'
      return report
    if restore_only and initial != CANDIDATE:
      report['status'] = 'unexpected_config'
      return report
    may_restore = restore_only

    stage = 'session_enter'
    session_attempted = True
    client.diagnostic_session_control(0x03)
    session_entered = True
    if not restore_only:
      stage = 'extended_config_read'
      extended = client.read_data_by_identifier(DATA_ID)
      report['extended_config_hex'] = extended.hex()
      if extended != ORIGINAL:
        report['status'] = 'extended_config_mismatch'
        return report
      stage = 'candidate_write'
      report['write_attempted'] = True
      may_restore = True
      client.write_data_by_identifier(DATA_ID, CANDIDATE)
      report['write_acknowledged'] = True
      stage = 'candidate_readback'
      observed = client.read_data_by_identifier(DATA_ID)
      report['candidate_readback_hex'] = observed.hex()
      report['status'] = 'candidate_accepted' if observed == CANDIDATE else 'candidate_readback_mismatch'
  except Exception as error:
    report['errors'].append(error_record(stage, error))
    nrc = getattr(error, 'error_code', None)
    if stage == 'candidate_write' and nrc == 0x31:
      report['status'] = 'write_rejected_ambiguous'
    elif stage == 'candidate_write' and nrc == 0x13:
      report['status'] = 'format_rejected'
    elif stage == 'candidate_write' and nrc in (0x22, 0x33, 0x35, 0x36, 0x37):
      report['status'] = 'prerequisite_blocked'
    else:
      report['status'] = 'diagnostic_error'
  finally:
    if firmware_matched:
      try:
        current = client.read_data_by_identifier(DATA_ID)
        report['pre_restore_config_hex'] = current.hex()
        if current != ORIGINAL and may_restore:
          if not session_entered:
            session_attempted = True
            client.diagnostic_session_control(0x03)
            session_entered = True
          report['restore_write_attempted'] = True
          client.write_data_by_identifier(DATA_ID, ORIGINAL)
        final = client.read_data_by_identifier(DATA_ID)
        report['final_config_hex'] = final.hex()
        report['final_config_verified'] = final == ORIGINAL
      except Exception as error:
        report['errors'].append(error_record('restore_config', error))
      if session_attempted:
        try:
          client.diagnostic_session_control(0x01)
          report['default_session_restored'] = True
        except Exception as error:
          report['errors'].append(error_record('session_restore', error))
      else:
        report['default_session_restored'] = True
      report['restore_status'] = ('confirmed' if report['final_config_verified'] and report['default_session_restored']
                                  else 'unverified')
      if restore_only and report['restore_status'] == 'confirmed' and report['status'] == 'not_started':
        report['status'] = 'original_restored'
  return report


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--restore-only', action='store_true')
  parser.add_argument('--output', type=Path, required=True)
  args = parser.parse_args()
  if subprocess.run(['pidof', 'pandad'], check=False, stdout=subprocess.DEVNULL).returncode != 1:
    parser.error('pandad must be confirmed stopped before radar diagnostics')

  from opendbc.car.structs import CarParams
  from opendbc.car.uds import UdsClient
  from panda.python import Panda

  panda = Panda()
  try:
    panda.set_safety_mode(CarParams.SafetyModel.elm327)
    report = run_trial(UdsClient(panda, 0x7d0, bus=0), args.restore_only)
  finally:
    panda.close()
  args.output.parent.mkdir(parents=True, exist_ok=True)
  temp = args.output.with_suffix(args.output.suffix + '.tmp')
  temp.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
  temp.replace(args.output)
  os.chmod(args.output, 0o644)
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))
  if report['restore_status'] != 'confirmed':
    return 3
  return 0 if report['status'] in ('candidate_accepted', 'already_original', 'original_restored') else 2


if __name__ == '__main__':
  raise SystemExit(main())
