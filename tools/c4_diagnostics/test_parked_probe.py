# 6999 정차 진단 실행기가 서비스를 복구하고 결과를 기록하는지 검증한다.
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.c4_diagnostics import parked_probe


class TestParkedProbe(unittest.TestCase):
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
        self.assertEqual(parked_probe.run_worker(), 0)

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
        self.assertEqual(parked_probe.run_worker(), 0)

      status = json.loads((root / "status.json").read_text())
      self.assertEqual(status["probe_returncode"], 124)
      self.assertIn("restoration is unverified", status["error"])
      self.assertTrue(status["comma_restarted"])


if __name__ == "__main__":
  unittest.main()
