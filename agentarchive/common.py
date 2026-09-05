"""Shared constants, bounded formatting, hashing, and filesystem primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import uuid
from typing import Any, Dict, Optional, Sequence




VERSION = "1.6.1"
LABEL_CAPTURE = "org.agentarchive.capture"
LABEL_VERIFY = "org.agentarchive.verify"
KEYCHAIN_SERVICE = "org.agentarchive.restic-local"
KEYCHAIN_ACCOUNT = "agentarchive"
HOME = pathlib.Path.home()
DEFAULT_STATE = HOME / "Library" / "Application Support" / "AgentArchive"
DEFAULT_CONFIG = DEFAULT_STATE / "config.json"
DEFAULT_SOURCE_ROOT = pathlib.Path(__file__).resolve().parent.parent
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
URL_RE = re.compile(r"https?://[^\s<>\]\[()\"']+")
GITHUB_PR_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.I)
GITHUB_ISSUE_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)", re.I)
COMMIT_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", re.I)
SUCCESSFUL_CAPTURE_STATUSES = ("complete", "local_complete_offsite_failed")
DURABLE_CAPTURE_STATUSES = SUCCESSFUL_CAPTURE_STATUSES + (
    "recovered_orphan",
    "recovered_orphan_offsite_failed",
    "orphan_recovery_pending_cleanup",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def capture_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def compact(text: str, limit: int = 2000) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def progress(message: str) -> None:
    print(f"AgentArchive [{utc_now()}] {message}", file=sys.stderr, flush=True)


def md_cell(text: str, limit: int = 180) -> str:
    return compact(text or "", limit).replace("|", "&#124;")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def write_json(path: pathlib.Path, value: Any, mode: int = 0o600) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), mode)


def read_json(path: pathlib.Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_private_tree(state: pathlib.Path) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    for rel in (
        "audits",
        "audits/staging",
        "bin",
        "logs",
        "manifests",
        "metadata-supplements",
        "queue",
        "staging",
        "staging-trash",
        "indexes",
        "indexes/sessions",
    ):
        path = state / rel
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)


def run(
    command: Sequence[str],
    *,
    cwd: Optional[pathlib.Path] = None,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
    capture: bool = True,
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        stderr = compact(result.stderr or "", 4000)
        stdout = compact(result.stdout or "", 2000)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command)}\n{stderr or stdout}")
    return result
