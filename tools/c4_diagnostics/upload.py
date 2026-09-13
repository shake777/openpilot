#!/usr/bin/env python3
# C4 진단 로그를 전용 운영 API에 안전하게 전송하는 독립 클라이언트
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_API_URL = "https://www.dayoutec.com/c4-diagnostics/api/v1/logs"
DEFAULT_CONFIG_PATH = Path("/data/c4-diagnostics.json")
MAX_FILES = 8
MAX_TOTAL_BYTES = 5 * 1024 * 1024
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UploadError(RuntimeError):
  pass


@dataclass(frozen=True)
class UploadConfig:
  api_url: str
  api_key: str
  auth_header: str = "X-C4-API-Key"
  auth_scheme: str = ""
  file_field: str = "files"
  source_id: str | None = None
  timeout: float = 60.0


def _read_config_file(path: Path | None) -> dict:
  if path is None or not path.exists():
    return {}
  if os.name != "nt" and path.stat().st_mode & 0o077:
    raise UploadError(f"configuration file must not be accessible by group or others: {path}")
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as exc:
    raise UploadError(f"failed to read configuration file: {path}") from exc
  if not isinstance(data, dict):
    raise UploadError("configuration must be a JSON object")
  return data


def load_config(path: Path | None = None) -> UploadConfig:
  configured_path = path
  if configured_path is None:
    env_path = os.environ.get("C4_DIAGNOSTICS_CONFIG")
    configured_path = Path(env_path) if env_path else DEFAULT_CONFIG_PATH
  values = _read_config_file(configured_path)

  def setting(env_name: str, file_name: str, default=None):
    return os.environ.get(env_name, values.get(file_name, default))

  api_key = setting("C4_DIAGNOSTICS_API_KEY", "api_key")
  if not api_key:
    raise UploadError("C4 diagnostics API key is not configured")

  config = UploadConfig(
    api_url=str(setting("C4_DIAGNOSTICS_API_URL", "api_url", DEFAULT_API_URL)),
    api_key=str(api_key),
    auth_header=str(setting("C4_DIAGNOSTICS_AUTH_HEADER", "auth_header", "X-C4-API-Key")),
    auth_scheme=str(setting("C4_DIAGNOSTICS_AUTH_SCHEME", "auth_scheme", "")),
    file_field=str(setting("C4_DIAGNOSTICS_FILE_FIELD", "file_field", "files")),
    source_id=setting("C4_DIAGNOSTICS_SOURCE_ID", "source_id"),
    timeout=float(setting("C4_DIAGNOSTICS_TIMEOUT", "timeout", 60.0)),
  )
  parsed_url = urlparse(config.api_url)
  local_http = parsed_url.hostname in {"127.0.0.1", "localhost"}
  if parsed_url.scheme != "https" and not (parsed_url.scheme == "http" and local_http):
    raise UploadError("API key transmission requires HTTPS")
  if not config.auth_header or "\n" in config.auth_header or "\r" in config.auth_header:
    raise UploadError("invalid authentication header name")
  return config


def validate_upload(source_id: str, upload_id: str, files: list[Path]) -> tuple[str, list[dict]]:
  if not SOURCE_ID_RE.fullmatch(source_id):
    raise UploadError("source_id must contain only letters, numbers, dot, underscore, or hyphen")
  try:
    normalized_upload_id = str(uuid.UUID(upload_id))
  except ValueError as exc:
    raise UploadError("upload_id must be a valid UUID") from exc
  if not 1 <= len(files) <= MAX_FILES:
    raise UploadError(f"between 1 and {MAX_FILES} files are required")

  file_info = []
  total_size = 0
  for path in files:
    if not path.is_file():
      raise UploadError(f"file does not exist or is not a regular file: {path}")
    size = path.stat().st_size
    total_size += size
    if total_size > MAX_TOTAL_BYTES:
      raise UploadError("total file size exceeds 5 MiB")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    file_info.append({"path": path, "name": path.name, "size": size, "sha256": digest})
  return normalized_upload_id, file_info


def _quoted(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def build_multipart(fields: dict[str, str], file_field: str, files: list[dict]) -> tuple[bytes, str]:
  boundary = f"c4-{uuid.uuid4().hex}"
  body = bytearray()
  for name, value in fields.items():
    body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quoted(name)}"\r\n\r\n'.encode())
    body.extend(value.encode("utf-8"))
    body.extend(b"\r\n")
  for info in files:
    body.extend(
      f'--{boundary}\r\nContent-Disposition: form-data; name="{_quoted(file_field)}"; '
      f'filename="{_quoted(info["name"])}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    )
    body.extend(info["path"].read_bytes())
    body.extend(b"\r\n")
  body.extend(f"--{boundary}--\r\n".encode())
  return bytes(body), boundary


def upload(config: UploadConfig, source_id: str, upload_id: str, paths: list[Path], optional_fields: dict[str, str]) -> dict:
  normalized_upload_id, file_info = validate_upload(source_id, upload_id, paths)
  fields = {"source_id": source_id, "upload_id": normalized_upload_id}
  fields.update({key: value for key, value in optional_fields.items() if value})
  body, boundary = build_multipart(fields, config.file_field, file_info)
  auth_value = f"{config.auth_scheme} {config.api_key}".strip()
  request = Request(config.api_url, data=body, method="POST", headers={
    config.auth_header: auth_value,
    "Content-Type": f"multipart/form-data; boundary={boundary}",
    "Accept": "application/json",
    "User-Agent": "openpilot-c4-diagnostics/1",
  })
  try:
    with urlopen(request, timeout=config.timeout) as response:
      response_body = response.read().decode("utf-8")
      status = response.status
  except HTTPError as exc:
    response_body = exc.read().decode("utf-8", errors="replace")
    raise UploadError(f"server rejected upload with HTTP {exc.code}: {response_body[:500]}") from exc
  except URLError as exc:
    raise UploadError(f"upload connection failed: {exc.reason}") from exc

  if status not in (200, 201):
    raise UploadError(f"unexpected HTTP status: {status}")
  try:
    receipt = json.loads(response_body)
  except json.JSONDecodeError as exc:
    raise UploadError("server returned a non-JSON receipt") from exc
  return {
    "upload_id": normalized_upload_id,
    "files": [{key: value for key, value in info.items() if key != "path"} for info in file_info],
    "receipt": receipt,
  }


def parse_args(argv=None):
  parser = argparse.ArgumentParser(description="Upload C4 diagnostic logs to the dedicated API")
  parser.add_argument("files", nargs="+", type=Path)
  parser.add_argument("--config", type=Path)
  parser.add_argument("--source-id")
  parser.add_argument("--upload-id", default=None)
  parser.add_argument("--site")
  parser.add_argument("--software-version")
  parser.add_argument("--collected-at")
  parser.add_argument("--note")
  return parser.parse_args(argv)


def main(argv=None) -> int:
  args = parse_args(argv)
  try:
    config = load_config(args.config)
    source_id = args.source_id or config.source_id
    if not source_id:
      raise UploadError("source_id is required in arguments, environment, or configuration")
    result = upload(config, source_id, args.upload_id or str(uuid.uuid4()), args.files, {
      "site": args.site,
      "software_version": args.software_version,
      "collected_at": args.collected_at,
      "note": args.note,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
  except UploadError as exc:
    print(f"C4 diagnostic upload failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
