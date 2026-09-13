# C4 진단 로그 업로더의 입력 제한과 multipart 전송 계약을 검증하는 테스트
import json
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.c4_diagnostics.upload import MAX_TOTAL_BYTES, UploadConfig, UploadError, upload, validate_upload


class TestC4DiagnosticsUpload(unittest.TestCase):
  def test_validate_upload_rejects_invalid_uuid(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      log_file = Path(temp_dir) / "c4.log"
      log_file.write_text("ok", encoding="utf-8")
      with self.assertRaisesRegex(UploadError, "UUID"):
        validate_upload("c4-001", "not-a-uuid", [log_file])

  def test_validate_upload_rejects_more_than_eight_files(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      files = []
      for index in range(9):
        path = Path(temp_dir) / f"{index}.log"
        path.write_bytes(b"x")
        files.append(path)
      with self.assertRaisesRegex(UploadError, "between 1 and 8"):
        validate_upload("c4-001", str(uuid.uuid4()), files)

  def test_validate_upload_rejects_total_over_five_mib(self):
    with tempfile.TemporaryDirectory() as temp_dir:
      log_file = Path(temp_dir) / "large.log"
      log_file.write_bytes(b"x" * (MAX_TOTAL_BYTES + 1))
      with self.assertRaisesRegex(UploadError, "5 MiB"):
        validate_upload("c4-001", str(uuid.uuid4()), [log_file])

  def test_upload_sends_auth_fields_and_original_bytes(self):
    received = {}

    class Handler(BaseHTTPRequestHandler):
      def do_POST(self):
        length = int(self.headers["Content-Length"])
        received["headers"] = self.headers
        received["body"] = self.rfile.read(length)
        payload = json.dumps({"ok": True, "receipt_id": "receipt-1"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

      def log_message(self, *_args):
        return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
      with tempfile.TemporaryDirectory() as temp_dir:
        original = b"raw\x00c4\xfflog"
        log_file = Path(temp_dir) / "tmux.log"
        log_file.write_bytes(original)
        upload_id = str(uuid.uuid4())
        result = upload(
          UploadConfig(f"http://127.0.0.1:{server.server_port}/logs", "secret-value"),
          "c4-001",
          upload_id,
          [log_file],
          {"site": "factory-a", "software_version": "carrot-wip-custom"},
        )
    finally:
      server.shutdown()
      server.server_close()

    self.assertEqual(received["headers"]["X-C4-API-Key"], "secret-value")
    self.assertIn(b'name="source_id"\r\n\r\nc4-001', received["body"])
    self.assertIn(b'name="upload_id"', received["body"])
    self.assertIn(b'name="files"; filename="tmux.log"', received["body"])
    self.assertIn(original, received["body"])
    self.assertEqual(result["upload_id"], upload_id)
    self.assertTrue(result["files"][0]["sha256"])
    self.assertEqual(result["receipt"]["receipt_id"], "receipt-1")


if __name__ == "__main__":
  unittest.main()
