# C4 레이더 캡처의 중복 없는 자동 전송 상태를 관리하는 모듈
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tools.c4_diagnostics.upload import UploadConfig, UploadError, upload


STATE_SCHEMA = 2
UPLOAD_NAMESPACE = uuid.UUID("c20ca0fb-17d8-4d20-9cba-280e2a8ccaca")


def save_state(path: Path, state: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  os.chmod(path.parent, 0o700)
  temporary = path.with_suffix(path.suffix + ".tmp")
  temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
  os.chmod(temporary, 0o600)
  os.replace(temporary, path)


def load_or_start_state(path: Path, now: float | None = None) -> dict:
  current_time = time.time() if now is None else now
  if path.exists():
    try:
      state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
      raise UploadError(f"failed to read automatic upload state: {path}") from exc
    if state.get("schema") not in {1, STATE_SCHEMA} or not isinstance(state.get("uploaded"), dict):
      raise UploadError("automatic upload state has an unsupported format")
    if state["schema"] == 1:
      state = {
        "schema": STATE_SCHEMA,
        "started_at": state.get("started_at", current_time),
        "uploaded": state["uploaded"],
      }
      save_state(path, state)
    return state
  state = {
    "schema": STATE_SCHEMA,
    "started_at": current_time,
    "uploaded": {},
  }
  save_state(path, state)
  return state


def deterministic_upload_id(source_id: str, capture: Path, sha256: str) -> str:
  return str(uuid.uuid5(UPLOAD_NAMESPACE, f"{source_id}\n{capture.name}\n{sha256}"))


def pending_captures(spool_dir: Path, state: dict) -> list[Path]:
  uploaded = state["uploaded"]
  return [path for path in sorted(spool_dir.glob("*.c4radar")) if path.name not in uploaded]


def upload_one(config: UploadConfig, source_id: str, state: dict, state_path: Path, capture: Path) -> dict:
  import hashlib

  digest = hashlib.sha256(capture.read_bytes()).hexdigest()
  upload_id = deterministic_upload_id(source_id, capture, digest)
  companion = capture.with_suffix(".meminfo")
  files = [capture] + ([companion] if companion.is_file() else [])
  collected_at = datetime.fromtimestamp(capture.stat().st_mtime, timezone.utc).isoformat()
  result = upload(config, source_id, upload_id, files, {
    "site": config.site,
    "software_version": config.software_version,
    "collected_at": collected_at,
    "note": config.note,
  })
  state["uploaded"][capture.name] = {
    "upload_id": upload_id,
    "sha256": digest,
    "uploaded_at": time.time(),
  }
  save_state(state_path, state)
  return result
