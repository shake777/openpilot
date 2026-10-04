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


# Uploaded bundles were never removed, so /data grew with every drive. Keep the
# spool under this size: oldest uploaded bundles go first, then the oldest
# not-yet-uploaded ones (e.g. while the server is unreachable for days).
MAX_SPOOL_BYTES = 512 * 1024 * 1024
BUNDLE_SUFFIXES = (".c4radar", ".c4scene", ".c4can.json", ".meminfo", ".qcamera.h264", ".qcamera.json", ".c4event.json")


def _bundle_files(spool_dir: Path) -> dict[str, list[Path]]:
  bundles: dict[str, list[Path]] = {}
  for path in spool_dir.iterdir():
    if path.is_file() and not path.name.endswith(".partial"):
      for suffix in BUNDLE_SUFFIXES:
        if path.name.endswith(suffix):
          bundles.setdefault(path.name[:-len(suffix)], []).append(path)
          break
  return bundles


def prune_spool(spool_dir: Path, state: dict, max_bytes: int = MAX_SPOOL_BYTES) -> int:
  """Delete whole bundles, oldest first, until the spool fits. Returns bytes freed."""
  if not spool_dir.is_dir():
    return 0
  bundles = _bundle_files(spool_dir)
  sizes = {name: sum(path.stat().st_size for path in files) for name, files in bundles.items()}
  total = sum(sizes.values())
  if total <= max_bytes:
    return 0
  uploaded = state["uploaded"]
  # Capture names start with a UTC timestamp, so name order is age order.
  order = sorted(bundles, key=lambda name: (f"{name}.c4radar" not in uploaded, name))
  freed = 0
  for name in order:
    if total - freed <= max_bytes:
      break
    for path in bundles[name]:
      path.unlink(missing_ok=True)
    freed += sizes[name]
  return freed


def pending_captures(spool_dir: Path, state: dict) -> list[Path]:
  uploaded = state["uploaded"]
  inventory = [path for path in sorted(spool_dir.glob("radar-inventory-*.json")) if path.name not in uploaded]
  probe = [path for path in sorted(spool_dir.glob("k7-security-probe-*.json")) if path.name not in uploaded]
  return inventory + probe + [path for path in sorted(spool_dir.glob("*.c4radar"))
          if path.name not in uploaded and path.with_suffix(".c4scene").is_file()]


def upload_one(config: UploadConfig, source_id: str, state: dict, state_path: Path, capture: Path) -> dict:
  import hashlib

  digest = hashlib.sha256(capture.read_bytes()).hexdigest()
  upload_id = deterministic_upload_id(source_id, capture, digest)
  scene = capture.with_suffix(".c4scene")
  companion = capture.with_suffix(".meminfo")
  inventory = capture.with_suffix(".c4can.json")
  video = capture.with_suffix(".qcamera.h264")
  video_metadata = capture.with_suffix(".qcamera.json")
  event = capture.with_suffix(".c4event.json")
  files = [capture]
  if capture.suffix == ".c4radar":
    files += ([scene] if scene.is_file() and scene.stat().st_size else []) \
             + ([companion] if companion.is_file() and companion.stat().st_size else []) \
             + ([inventory] if inventory.is_file() and inventory.stat().st_size else []) \
             + ([video] if video.is_file() and video.stat().st_size else []) \
             + ([video_metadata] if video_metadata.is_file() and video_metadata.stat().st_size else [])              + ([event] if event.is_file() and event.stat().st_size else [])
  collected_at = datetime.fromtimestamp(capture.stat().st_mtime, timezone.utc).isoformat()
  result = upload(config, source_id, upload_id, files, {
    "site": config.site,
    "software_version": config.software_version,
    "collected_at": collected_at,
    "note": config.note,
    "account": getattr(config, "account", ""),
  })
  state["uploaded"][capture.name] = {
    "upload_id": upload_id,
    "sha256": digest,
    "uploaded_at": time.time(),
  }
  save_state(state_path, state)
  return result
