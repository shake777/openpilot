# K7 레이더의 한정된 식별자와 DTC를 설정·세션·보안 변경 없이 수집한다.
import time

from openpilot.selfdrive.debug.car.hyundai_enable_radar_points import is_k7_experimental_firmware, K7_EXPERIMENTAL_CONFIG


CONFIG_DIDS = tuple(range(0x0140, 0x0149))
# Do not collect VIN or ECU serial numbers.
IDENTITY_DIDS = (0xf181, 0xf187, 0xf188, 0xf189, 0xf18a, 0xf18b)


def characterize(uds_client, bus=0):
  from opendbc.car.uds import DTC_REPORT_TYPE, DTC_STATUS_MASK_TYPE, MessageTimeoutError, NegativeResponseError

  report = {
    'schema': 'c4-k7-security-probe-v1', 'mode': 'k7-characterize-no-session-change',
    'created_at': time.time(), 'bus': bus, 'status': 'not_started',
    'write_performed': False, 'key_sent': False, 'seed_requested': False,
    'diagnostic_session_changed': False, 'restore_status': 'not_needed',
    'final_config_verified': False, 'dtc_changed': None, 'errors': [], 'reads': {},
  }

  def read(stage, action):
    time.sleep(0.05)
    started = time.monotonic()
    try:
      value = action()
    except (MessageTimeoutError, NegativeResponseError, ValueError) as error:
      entry = {'stage': stage, 'status': 'error', 'error_type': type(error).__name__, 'message': str(error)}
      if isinstance(error, NegativeResponseError):
        entry['nrc'] = f'0x{error.error_code:02x}'
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

  try:
    before = dtc('dtc_before')
    # F186 may be unsupported. No security request depends on it in this mode.
    did('active_session', 0xf186)
    for identifier in IDENTITY_DIDS + CONFIG_DIDS:
      did(f'did_{identifier:04x}', identifier)
    after = dtc('dtc_after')
    if before is not None and after is not None:
      # Raw changes require review; they do not by themselves prove new faults.
      report['dtc_changed'] = before != after
    report['status'] = 'collected_with_errors' if report['errors'] else 'collected'
  finally:
    final = did('final_config', 0x0142)
    report['post_restore_config_hex'] = final
    report['final_config_verified'] = final == original
  if not report['final_config_verified']:
    report['status'] = 'configuration_unverified'
    return report, 3
  if report['dtc_changed']:
    report['status'] = 'dtc_changed_review_required'
    return report, 3
  return report, 2 if report['errors'] else 0
