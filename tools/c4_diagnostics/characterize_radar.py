# K7 레이더의 한정된 식별자와 DTC를 읽고 선택적으로 기본·확장 세션을 비교한다.
import time

from openpilot.selfdrive.debug.car.hyundai_enable_radar_points import is_k7_experimental_firmware, K7_EXPERIMENTAL_CONFIG


CONFIG_DIDS = tuple(range(0x0140, 0x0149))
# Do not collect VIN or ECU serial numbers.
IDENTITY_DIDS = (0xf181, 0xf187, 0xf188, 0xf189, 0xf18a, 0xf18b)


def characterize(uds_client, bus=0, compare_sessions=False):
  from opendbc.car.uds import DTC_REPORT_TYPE, DTC_STATUS_MASK_TYPE, MessageTimeoutError, NegativeResponseError

  report = {
    'schema': 'c4-k7-security-probe-v1', 'mode': 'k7-characterize-no-session-change',
    'created_at': time.time(), 'bus': bus, 'status': 'not_started',
    'write_performed': False, 'key_sent': False, 'seed_requested': False,
    'diagnostic_session_changed': False, 'restore_status': 'not_needed',
    'final_config_verified': False, 'dtc_changed': None, 'errors': [], 'reads': {},
  }
  if compare_sessions:
    report['mode'] = 'k7-characterize-session-comparison'
    report['session_comparison'] = {}
    report['comparison_complete'] = False
  session_attempted = False

  def read(stage, action):
    time.sleep(0.05)
    started = time.monotonic()
    try:
      value = action()
    except (MessageTimeoutError, NegativeResponseError, ValueError) as error:
      entry = {'stage': stage, 'status': 'error', 'error_type': type(error).__name__, 'message': str(error)}
      if isinstance(error, NegativeResponseError):
        entry['nrc'] = f'0x{error.error_code:02x}'
      if isinstance(error, MessageTimeoutError):
        entry['timeout'] = True
      report['errors'].append(entry)
    else:
      entry = {'status': 'ok', 'raw_hex': value.hex(), 'bytes': len(value)}
    entry['elapsed_s'] = round(time.monotonic() - started, 3)
    report['reads'][stage] = entry
    return entry.get('raw_hex')

  def did(stage, identifier):
    return read(stage, lambda: uds_client.read_data_by_identifier(identifier))

  firmware = did('firmware', 0xf100)
  report['firmware_hex'] = firmware
  if firmware is None or not is_k7_experimental_firmware(bytes.fromhex(firmware)):
    report['status'] = 'firmware_unverified'
    return report, 5
  original = did('initial_config', 0x0142)
  report['default_config_hex'] = original
  if original != K7_EXPERIMENTAL_CONFIG.default_config.hex():
    report['status'] = 'configuration_unverified'
    return report, 3

  def dtc(stage):
    return read(stage, lambda: uds_client.read_dtc_information(DTC_REPORT_TYPE.DTC_BY_STATUS_MASK, DTC_STATUS_MASK_TYPE.ALL))

  def session(stage, value):
    try:
      uds_client.diagnostic_session_control(value)
    except (MessageTimeoutError, NegativeResponseError) as error:
      entry = {'stage': stage, 'error_type': type(error).__name__, 'message': str(error)}
      if isinstance(error, NegativeResponseError):
        entry['nrc'] = f'0x{error.error_code:02x}'
      report['errors'].append(entry)
      return False
    return True

  before = None
  try:
    if compare_sessions:
      session_attempted = True
      report['session_change_attempted'] = True
      report['default_session_confirmed'] = session('default_session_enter', 0x01)
    if not compare_sessions or report['default_session_confirmed']:
      before = dtc('dtc_before')
      # F186 may be unsupported. No security request depends on it in this mode.
      did('active_session', 0xf186)
      for identifier in IDENTITY_DIDS + CONFIG_DIDS:
        did(f'did_{identifier:04x}', identifier)
      report['baseline_config_verified'] = report['reads']['did_0142'].get('raw_hex') == original
    if compare_sessions and report['default_session_confirmed'] and report['baseline_config_verified']:
      entered = session('extended_session_enter', 0x03)
      report['diagnostic_session_changed'] = entered
      report['extended_config_verified'] = entered and did('extended_config', 0x0142) == original
      if report['extended_config_verified']:
        for identifier in IDENTITY_DIDS + CONFIG_DIDS:
          name = f'did_{identifier:04x}'
          did(f'extended_{name}', identifier)
          initial, extended = report['reads'][name], report['reads'][f'extended_{name}']
          if extended['status'] == 'ok':
            result = ('baseline_unverified' if initial['status'] != 'ok' and 'nrc' not in initial else
                      'newly_readable' if initial['status'] != 'ok' else
                      'unchanged' if initial['raw_hex'] == extended['raw_hex'] else 'value_changed')
          else:
            result = 'security_denied' if extended.get('nrc') == '0x33' else 'unavailable'
          report['session_comparison'][f'0x{identifier:04x}'] = result
          if extended.get('timeout'):
            # Do not label later reads as extended-session evidence after a lost response.
            break
  finally:
    if session_attempted:
      restored = session('default_session_restore', 0x01)
      report['default_session_restored'] = restored
      report['restore_status'] = 'confirmed' if restored else 'unconfirmed'
    final = did('final_config', 0x0142)
    report['post_restore_config_hex'] = final
    report['final_config_verified'] = final == original
    after = dtc('dtc_after')
    if before is not None and after is not None:
      # Raw changes require review; they do not by themselves prove new faults.
      report['dtc_changed'] = before != after
  if session_attempted and report['restore_status'] != 'confirmed':
    report['status'] = 'restore_unverified'
    return report, 3
  if compare_sessions and not report['default_session_confirmed']:
    report['status'] = 'default_session_unverified'
    return report, 5
  if compare_sessions and not report['baseline_config_verified']:
    report['status'] = 'default_configuration_unverified'
    return report, 3
  if session_attempted and report['diagnostic_session_changed'] and not report['extended_config_verified']:
    report['status'] = 'extended_configuration_unverified'
    return report, 3
  if not report['final_config_verified']:
    report['status'] = 'configuration_unverified'
    return report, 3
  if report['dtc_changed']:
    report['status'] = 'dtc_changed_review_required'
    return report, 3
  report['status'] = 'collected_with_errors' if report['errors'] else 'collected'
  if compare_sessions:
    report['comparison_complete'] = len(report['session_comparison']) == len(IDENTITY_DIDS + CONFIG_DIDS)
  return report, 2 if report['errors'] else 0
