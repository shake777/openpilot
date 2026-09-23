# 6999 정차 진단 실행기가 서비스를 복구하고 결과를 기록하는지 검증한다.
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.c4_diagnostics import parked_probe
from tools.c4_diagnostics.auto_upload import pending_captures


class TestParkedProbe(unittest.TestCase):
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
