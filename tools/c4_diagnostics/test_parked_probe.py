# 6999 정차 진단 실행기가 서비스를 복구하고 결과를 기록하는지 검증한다.
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.c4_diagnostics import parked_probe
from tools.c4_diagnostics.auto_upload import pending_captures, upload_one
from tools.c4_diagnostics.upload import UploadConfig, UploadError


class TestParkedProbe(unittest.TestCase):
  def test_batch_reports_auto_upload_and_retry(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      spool = root / 'spool'
      spool.mkdir()
      reports = [spool / 'k7-security-probe-session.json', spool / 'k7-security-probe-security.json',
                 spool / 'k7-security-probe-status-test.json']
      for report in reports:
        report.write_text('{"status":"saved"}', encoding='utf-8')
      state = {'schema': 2, 'uploaded': {}}
      state_path = root / 'state.json'
      config = UploadConfig('https://www.dayoutec.com/c4-diagnostics/api/v1/logs', 'test-key')
      with patch('tools.c4_diagnostics.auto_upload.upload', side_effect=UploadError('offline')):
        with self.assertRaises(UploadError):
          upload_one(config, 'c4-test', state, state_path, pending_captures(spool, state)[0])
      self.assertEqual(len(pending_captures(spool, state)), 3)
      with patch('tools.c4_diagnostics.auto_upload.upload', return_value={'upload_id': 'received'}) as send:
        while pending_captures(spool, state):
          upload_one(config, 'c4-test', state, state_path, pending_captures(spool, state)[0])
      self.assertEqual(send.call_count, 3)
      self.assertEqual({call.args[3][0] for call in send.call_args_list}, set(reports))
      self.assertEqual(pending_captures(spool, state), [])
      self.assertEqual(set(json.loads(state_path.read_text())['uploaded']), {report.name for report in reports})

  def test_batch_survey_launch_requires_park_and_selects_one_worker(self):
    with patch.object(Path, 'exists', return_value=True), \
         patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=True), \
         patch('builtins.input', side_effect=AssertionError('text prompt')), \
         patch.object(parked_probe, 'run_command', return_value=SimpleNamespace(returncode=0)) as run:
      self.assertEqual(parked_probe.start_from_web(batch_survey=True), 0)
    command = run.call_args.args[0]
    self.assertIn('--batch-survey', command)
    self.assertIn('--verify-parked', command)
    self.assertNotIn('--candidate-trial', command)

  def test_batch_survey_runs_two_read_only_steps_in_one_service_window(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      reports = [root / 'spool/k7-security-probe-session.json', root / 'spool/k7-security-probe-security.json']
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        if '--k7-characterize' in command or '--security-survey' in command:
          path = reports[0] if '--k7-characterize' in command else reports[1]
          path.parent.mkdir(exist_ok=True)
          path.write_text(json.dumps({'status': 'collected_with_errors', 'restore_status': 'confirmed',
                                      'final_config_verified': True, 'dtc_changed': False}))
        return SimpleNamespace(returncode=2 if '--k7-characterize' in command else 0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'probe_report_path', side_effect=reports), \
           patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=True), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(verify_parked=True, batch_survey=True), 0)
      status = json.loads((root / 'status.json').read_text())
      self.assertEqual([step['name'] for step in status['batch_steps']], ['session_comparison', 'security_survey'])
      self.assertEqual(len([call for call in calls if call[:3] == ['systemctl', 'stop', 'comma']]), 1)
      self.assertEqual(len(pending_captures(reports[0].parent, {'uploaded': {}})), 3)

  def test_batch_survey_stops_after_unverified_restore(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      report = root / 'spool/session.json'
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        if '--k7-characterize' in command:
          report.parent.mkdir(exist_ok=True)
          report.write_text(json.dumps({'status': 'restore_unverified', 'restore_status': 'unconfirmed',
                                        'final_config_verified': False}))
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'probe_report_path', return_value=report), \
           patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=True), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(verify_parked=True, batch_survey=True), 3)
      self.assertFalse(any('--security-survey' in call for call in calls))
      self.assertIn('remaining steps skipped', json.loads((root / 'status.json').read_text())['error'])

  def test_security_survey_command_and_summary(self):
    command = parked_probe.probe_command(Path('/tmp/report.json'), security_survey=True)
    self.assertIn('--security-survey', command)
    self.assertNotIn('--restore-only', command)
    report = {'mode': 'security_survey', 'status': 'seed_rejected', 'security_level': '0x03',
              'key_sent': False, 'write_performed': False, 'initial_config_hex': '0002000000',
              'extended_config_hex': '0002000000', 'final_config_hex': '0002000000',
              'final_config_verified': True, 'errors': [{'stage': 'security_seed_03', 'nrc': '0x31'}]}
    summary = parked_probe.summarize_report(report)
    self.assertEqual(summary['error_counts'], {'0x31': 1})
    self.assertEqual(summary['security']['status'], 'seed_rejected')
    self.assertTrue(summary['security']['configuration_unchanged_verified'])
    self.assertFalse(summary['security']['key_sent'])

  def test_boot_summary_runs_once_after_success_and_retries_failure(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      status = root / 'status.json'
      status.write_text('{}')
      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', status), \
           patch.object(parked_probe, 'summarize_last', side_effect=[2, 0]) as summarize:
        self.assertEqual(parked_probe.summarize_last_once(root / 'spool'), 2)
        self.assertFalse((root / 'saved-summary-v1.done').exists())
        self.assertEqual(parked_probe.summarize_last_once(root / 'spool'), 0)
        self.assertTrue((root / 'saved-summary-v1.done').exists())
        self.assertEqual(parked_probe.summarize_last_once(root / 'spool'), 0)
        self.assertEqual(summarize.call_count, 2)
        summarize.assert_called_with(root / 'spool')

  def test_boot_without_saved_report_does_not_mark_completion(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'missing.json'), \
           patch.object(parked_probe, 'summarize_last') as summarize:
        self.assertEqual(parked_probe.summarize_last_once(root / 'spool'), 2)
        summarize.assert_not_called()
        self.assertFalse((root / 'saved-summary-v1.done').exists())

  def test_summary_preserves_values_rejections_and_missing_extended_read(self):
    report = {'reads': {'did_0140': {'status': 'ok', 'raw_hex': '0102'},
                        'extended_did_0140': {'status': 'ok', 'raw_hex': '0102'},
                        'did_0145': {'status': 'error', 'nrc': '0x31'}},
              'errors': [{'nrc': '0x31', 'stage': 'did_0145'}] * 21}
    summary = parked_probe.summarize_report(report)
    self.assertEqual(summary['error_counts'], {'0x31': 21})
    self.assertEqual(summary['did_values']['0x0140']['default']['raw_hex'], '0102')
    self.assertEqual(summary['did_values']['0x0145']['default']['nrc'], '0x31')
    self.assertEqual(summary['did_values']['0x0145']['extended'], {})
    self.assertEqual(summary['security'], {})

  def test_summary_distinguishes_unsupported_f186_from_accepted_seed(self):
    report = {'status': 'seed_accepted', 'security_level': '0x01', 'seed_hex': 'c17c', 'seed_length': 2,
              'session_confirmation_source': 'extended_response_and_0142',
              'session_keepalive_confirmed': True, 'key_sent': False, 'write_performed': False,
              'default_config_hex': '0002000000', 'extended_config_hex': '0002000000',
              'post_config_hex': '0002000000', 'post_restore_config_hex': '0002000000',
              'final_config_verified': True,
              'errors': [{'stage': 'session_read', 'nrc': '0x31'}]}
    summary = parked_probe.summarize_report(report)
    self.assertEqual(summary['error_counts'], {'0x31': 1})
    self.assertEqual(summary['security'], {
      'status': 'seed_accepted', 'security_level': '0x01', 'seed_length': 2,
      'session_confirmation_source': 'extended_response_and_0142',
      'session_keepalive_confirmed': True, 'key_sent': False, 'write_performed': False,
      'f186_unsupported': True, 'configuration_unchanged_verified': True})
    self.assertNotIn('c17c', json.dumps(summary))
    report['post_restore_config_hex'] = None
    self.assertIsNone(parked_probe.summarize_report(report)['security']['configuration_unchanged_verified'])

  def test_saved_summary_is_uploaded_without_running_vehicle_commands(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      report = root / 'report.json'
      report.write_text(json.dumps({'reads': {'did_0142': {'status': 'ok', 'raw_hex': '0002000000'}}}))
      status_path = root / 'status.json'
      original = json.dumps({'report_path': str(report), 'restore_status': 'unconfirmed',
                             'final_config_verified': False, 'report_status': 'restore_unverified'})
      status_path.write_text(original)
      with patch.object(parked_probe, 'STATUS_FILE', status_path), \
           patch.object(parked_probe, 'probe_report_path', return_value=root / 'spool/unused.json'), \
           patch.object(parked_probe, 'run_command') as run:
        self.assertEqual(parked_probe.summarize_last(), 0)
        run.assert_not_called()
      queued = pending_captures(root / 'spool', {'uploaded': {}})
      self.assertEqual(len(queued), 1)
      status = json.loads(queued[0].read_text())['worker_status']
      self.assertEqual(status['restore_status'], 'unconfirmed')
      self.assertFalse(status['final_config_verified'])
      self.assertEqual(status['summary']['did_values']['0x0142']['default']['raw_hex'], '0002000000')
      self.assertEqual(status_path.read_text(), original)

  def test_missing_saved_report_does_not_queue_success(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      status = root / 'status.json'
      status.write_text('{}')
      with patch.object(parked_probe, 'STATUS_FILE', status), patch.object(parked_probe, 'probe_report_path') as path:
        self.assertEqual(parked_probe.summarize_last(), 2)
        path.assert_not_called()

  def test_recovery_launch_skips_text_prompt_and_refuses_unverified_vehicle(self):
    with patch.object(parked_probe, 'PROBE', Path(__file__)), patch.object(Path, 'exists', return_value=True), \
         patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=False), \
         patch.object(parked_probe, 'record_preflight_block') as record, \
         patch('builtins.input', side_effect=AssertionError('text prompt')), \
         patch.object(parked_probe, 'run_command') as run:
      self.assertEqual(parked_probe.start_from_web(from_recovery=True), 2)
      run.assert_not_called()
      record.assert_called_once()

  def test_worker_rechecks_parked_state_before_stopping_comma(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=False), \
           patch.object(parked_probe, 'run_command', side_effect=run), patch.object(parked_probe.time, 'sleep'):
        self.assertEqual(parked_probe.run_worker(verify_parked=True), 3)
      self.assertEqual(calls, [])
      self.assertIn('state changed', json.loads((root / 'status.json').read_text())['error'])

  def test_recovery_launch_checks_live_vehicle_state(self):
    for gear, speed, rpm, started, expected in (('park', 0, 0, True, True),
                                                ('drive', 0, 0, True, False),
                                                ('park', 1, 0, True, False),
                                                ('park', 0, 800, True, False),
                                                ('park', 0, 0, False, False)):
      with self.subTest(gear=gear, speed=speed, rpm=rpm, started=started):
        messages = {'carState': SimpleNamespace(gearShifter=gear, vEgo=speed, engineRpm=rpm),
                    'deviceState': SimpleNamespace(started=started)}
        class FakeSubMaster:
          alive = {'carState': True, 'deviceState': True}
          valid = {'carState': True, 'deviceState': True}

          def update(self, _timeout):
            pass

          def __getitem__(self, key):
            return messages[key]

        sm = FakeSubMaster()
        cereal = SimpleNamespace(messaging=SimpleNamespace(SubMaster=lambda _, **kwargs: sm))
        with patch.dict(sys.modules, {'openpilot.cereal': cereal}):
          self.assertEqual(parked_probe.vehicle_ready_for_probe(), expected)

  def test_candidate_launch_requires_live_parked_state(self):
    with patch.object(Path, 'exists', return_value=True), \
         patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=False), \
         patch.object(parked_probe, 'record_preflight_block'), \
         patch('builtins.input', side_effect=AssertionError('text prompt')), \
         patch.object(parked_probe, 'run_command') as run:
      self.assertEqual(parked_probe.start_from_web(candidate_trial=True), 2)
      run.assert_not_called()

  def test_security_survey_launch_requires_park_and_selects_seed_only_mode(self):
    with patch.object(Path, 'exists', return_value=True), \
         patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=False), \
         patch.object(parked_probe, 'record_preflight_block'), \
         patch('builtins.input', side_effect=AssertionError('text prompt')), \
         patch.object(parked_probe, 'run_command') as run:
      self.assertEqual(parked_probe.start_from_web(security_survey=True), 2)
      run.assert_not_called()
    with patch.object(Path, 'exists', return_value=True), \
         patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=True), \
         patch('builtins.input', side_effect=AssertionError('text prompt')), \
         patch.object(parked_probe, 'run_command', return_value=SimpleNamespace(returncode=0)) as run:
      self.assertEqual(parked_probe.start_from_web(security_survey=True), 0)
      command = run.call_args.args[0]
      self.assertIn('--security-survey', command)
      self.assertIn('--verify-parked', command)
      self.assertNotIn('--candidate-trial', command)

  def test_candidate_worker_runs_independent_restore_and_queues_both_reports(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      primary = root / 'spool/k7-security-probe-trial.json'
      recovery = root / 'spool/k7-security-probe-recovery.json'
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        if '--restore-only' in command:
          recovery.parent.mkdir(exist_ok=True)
          recovery.write_text(json.dumps({'status': 'already_original', 'restore_status': 'confirmed',
                                          'final_config_verified': True, 'default_session_restored': True}))
        elif str(parked_probe.CANDIDATE_TRIAL) in command:
          primary.parent.mkdir(exist_ok=True)
          primary.write_text(json.dumps({'status': 'candidate_accepted', 'restore_status': 'confirmed',
                                         'final_config_verified': True}))
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'probe_report_path', side_effect=[primary, recovery]), \
           patch.object(parked_probe, 'vehicle_ready_for_probe', return_value=True), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(candidate_trial=True, verify_parked=True), 0)
      status = json.loads((root / 'status.json').read_text())
      self.assertEqual(status['mode'], 'candidate_trial')
      self.assertEqual(status['restore_status'], 'confirmed')
      self.assertTrue(status['final_config_verified'])
      self.assertTrue(status['comma_restarted'])
      self.assertEqual(len(pending_captures(primary.parent, {'uploaded': {}})), 3)
      self.assertIn('--restore-only', calls[2])

  def test_candidate_worker_does_not_claim_success_when_trial_report_is_missing(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      primary = root / 'spool/k7-security-probe-trial.json'
      recovery = root / 'spool/k7-security-probe-recovery.json'

      def run(command, **_kwargs):
        if '--restore-only' in command:
          recovery.parent.mkdir(exist_ok=True)
          recovery.write_text(json.dumps({'status': 'already_original', 'restore_status': 'confirmed',
                                          'final_config_verified': True, 'default_session_restored': True}))
          return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=124 if str(parked_probe.CANDIDATE_TRIAL) in command else 0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'probe_report_path', side_effect=[primary, recovery]), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(candidate_trial=True), 3)
      status = json.loads((root / 'status.json').read_text())
      self.assertTrue(status['final_config_verified'])
      self.assertEqual(status['restore_status'], 'confirmed')
      self.assertIn('timed out', status['error'])
      self.assertTrue(status['trial_report_missing'])
      self.assertFalse(primary.exists())

  def test_preflight_waits_for_delayed_state_and_reports_invalid_stream(self):
    for ready_after in (5, 100):
      with self.subTest(ready_after=ready_after):
        class FakeSubMaster:
          alive = {'carState': False, 'deviceState': True}
          valid = {'carState': False, 'deviceState': True}
          updates = 0

          def update(self, _timeout):
            self.updates += 1
            self.alive['carState'] = self.valid['carState'] = self.updates >= ready_after

          def __getitem__(self, key):
            return SimpleNamespace(gearShifter='park', vEgo=0, engineRpm=0, started=True)

        sm = FakeSubMaster()
        def submaster(services, **kwargs):
          self.assertEqual(kwargs['poll'], 'deviceState')
          return sm
        cereal = SimpleNamespace(messaging=SimpleNamespace(SubMaster=submaster))
        evidence = {}
        with patch.dict(sys.modules, {'openpilot.cereal': cereal}):
          self.assertEqual(parked_probe.vehicle_ready_for_probe(evidence), ready_after == 5)
        self.assertEqual(sm.updates, min(ready_after, 10))
        self.assertEqual(evidence['reasons'], [] if ready_after == 5 else ['carState unavailable or invalid'])

  def test_preflight_block_is_discovered_by_uploader(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      evidence = {'reasons': ['gear is not P'], 'gear': 'drive'}
      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'probe_report_path', return_value=root / 'spool/unused.json'):
        parked_probe.record_preflight_block(evidence, True, True)
      queued = pending_captures(root / 'spool', {'uploaded': {}})
      self.assertEqual(len(queued), 1)
      status = json.loads(queued[0].read_text())['worker_status']
      self.assertEqual(status['preflight'], evidence)
      self.assertFalse(status['probe_started'])
      self.assertFalse(status['comma_restarted'])
      self.assertTrue(status['compare_sessions'])
      self.assertEqual(status, json.loads((root / 'status.json').read_text()))

  def test_characterize_command_cannot_select_security_or_write_mode(self):
    command = parked_probe.probe_command(Path('/tmp/report.json'), characterize=True)
    self.assertIn('--k7-characterize', command)
    self.assertNotIn('--k7-security-probe', command)
    self.assertNotIn('--k7-experimental', command)

  def test_session_comparison_report_is_uploaded_with_restore_status(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      report_path = root / 'spool/k7-security-probe-test.json'
      comparison = {'0x0140': 'newly_readable'}

      def run(command, **_kwargs):
        if 'runuser' in command:
          self.assertIn('--k7-characterize', command)
          self.assertIn('--compare-sessions', command)
          self.assertNotIn('--k7-security-probe', command)
          report_path.parent.mkdir()
          report_path.write_text(json.dumps({'restore_status': 'confirmed', 'final_config_verified': True,
                                            'dtc_changed': False, 'session_comparison': comparison}))
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), patch.object(parked_probe, 'probe_report_path', return_value=report_path), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(characterize=True, compare_sessions=True), 0)
      queued = pending_captures(report_path.parent, {'uploaded': {}})
      self.assertEqual(len(queued), 2)
      uploaded = next(json.loads(p.read_text()) for p in queued if 'status-' in p.name)
      self.assertEqual(uploaded['worker_status']['session_comparison'], comparison)
      self.assertTrue(uploaded['worker_status']['comma_restarted'])

  def test_probe_records_restore_result_and_restarts_comma(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      report_path = root / "spool" / "k7-security-probe-test.json"
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        if "runuser" in command:
          report_path.parent.mkdir()
          report_path.write_text(json.dumps({"restore_status": "confirmed", "final_config_verified": True}))
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, "RESULT_DIR", root), patch.object(parked_probe, "STATUS_FILE", root / "status.json"), \
           patch.object(parked_probe, "LOG_FILE", root / "probe.log"), patch.object(parked_probe, "probe_report_path", return_value=report_path), \
           patch.object(parked_probe, "wait_for_pandad", return_value=True), patch.object(parked_probe.time, "sleep"), \
           patch.object(parked_probe, "run_command", side_effect=run):
        self.assertEqual(parked_probe.run_worker(), 0)

      status = json.loads((root / "status.json").read_text())
      self.assertTrue(status["probe_started"])
      self.assertEqual(status["restore_status"], "confirmed")
      self.assertTrue(status["final_config_verified"])
      self.assertTrue(status["comma_restarted"])
      self.assertEqual(calls[0], ["systemctl", "stop", "comma"])
      self.assertEqual(calls[-2], ["systemctl", "start", "comma"])
      self.assertEqual(calls[-1], ["systemctl", "is-active", "--quiet", "comma"])
      self.assertIn("--k7-security-probe", calls[1])
      self.assertNotIn("--k7-experimental", calls[1])
      queued = pending_captures(report_path.parent, {"uploaded": {}})
      self.assertEqual(len(queued), 2)
      uploaded_status = json.loads(next(path for path in queued if path != report_path).read_text())
      self.assertEqual(uploaded_status["worker_status"], status)
      self.assertIn('RESULT', uploaded_status['log_tail'])

  def test_pandad_still_running_skips_probe_but_restarts_comma(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, "RESULT_DIR", root), patch.object(parked_probe, "STATUS_FILE", root / "status.json"), \
           patch.object(parked_probe, "LOG_FILE", root / "probe.log"), patch.object(parked_probe, "wait_for_pandad", return_value=False), \
           patch.object(parked_probe.time, "sleep"), patch.object(parked_probe, "run_command", side_effect=run):
        self.assertEqual(parked_probe.run_worker(), 3)

      status = json.loads((root / "status.json").read_text())
      self.assertFalse(status["probe_started"])
      self.assertIn("pandad did not stop", status["error"])
      self.assertEqual(calls, [["systemctl", "stop", "comma"], ["systemctl", "start", "comma"],
                               ["systemctl", "is-active", "--quiet", "comma"]])

  def test_probe_timeout_records_unverified_session_and_restarts_comma(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)

      def run(command, **_kwargs):
        return SimpleNamespace(returncode=124 if command[0] == "timeout" else 0)

      with patch.object(parked_probe, "RESULT_DIR", root), patch.object(parked_probe, "STATUS_FILE", root / "status.json"), \
           patch.object(parked_probe, "LOG_FILE", root / "probe.log"), \
           patch.object(parked_probe, "probe_report_path", return_value=root / "missing.json"), \
           patch.object(parked_probe, "wait_for_pandad", return_value=True), \
           patch.object(parked_probe.time, "sleep"), patch.object(parked_probe, "run_command", side_effect=run):
        self.assertEqual(parked_probe.run_worker(), 3)

      status = json.loads((root / "status.json").read_text())
      self.assertEqual(status["probe_returncode"], 124)
      self.assertIn("restoration is unverified", status["error"])
      self.assertTrue(status["comma_restarted"])
      queued = pending_captures(root, {"uploaded": {}})
      self.assertEqual(len(queued), 1)
      self.assertEqual(json.loads(queued[0].read_text())["worker_status"], status)

  def test_characterization_report_is_queued_and_partial_result_preserved(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      report_path = root / 'spool/k7-security-probe-test.json'
      calls = []

      def run(command, **_kwargs):
        calls.append(command)
        if 'runuser' in command:
          report_path.parent.mkdir()
          report_path.write_text(json.dumps({'restore_status': 'not_needed', 'final_config_verified': True,
                                            'dtc_changed': None, 'status': 'collected_with_errors'}))
          return SimpleNamespace(returncode=2)
        return SimpleNamespace(returncode=0)

      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), patch.object(parked_probe, 'probe_report_path', return_value=report_path), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', side_effect=run):
        self.assertEqual(parked_probe.run_worker(characterize=True), 2)
      status = json.loads((root / 'status.json').read_text())
      self.assertEqual(status['mode'], 'characterize')
      self.assertEqual(status['report_status'], 'collected_with_errors')
      self.assertIn('--k7-characterize', calls[1])
      self.assertTrue(status['comma_restarted'])

  def test_missing_report_is_not_success_even_if_comma_restarts(self):
    with TemporaryDirectory() as directory:
      root = Path(directory)
      with patch.object(parked_probe, 'RESULT_DIR', root), patch.object(parked_probe, 'STATUS_FILE', root / 'status.json'), \
           patch.object(parked_probe, 'LOG_FILE', root / 'probe.log'), \
           patch.object(parked_probe, 'probe_report_path', return_value=root / 'missing.json'), \
           patch.object(parked_probe, 'wait_for_pandad', return_value=True), patch.object(parked_probe.time, 'sleep'), \
           patch.object(parked_probe, 'run_command', return_value=SimpleNamespace(returncode=0)):
        self.assertEqual(parked_probe.run_worker(characterize=True), 3)
      self.assertIn('report missing', json.loads((root / 'status.json').read_text())['error'])


if __name__ == "__main__":
  unittest.main()
