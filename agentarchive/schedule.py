"""Fast session-end checkpoints and power-aware scheduled operations."""

from __future__ import annotations

import contextlib
import json
import pathlib
import re
import sys
import uuid
from privacy_filter import transcript_is_excluded, workspace_is_excluded
from typing import Any, Dict, List, Optional, Tuple

from . import capture, catalog, common as utils, config as configuration, reports, storage


def enqueue_hook(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = sys.stdin.buffer.read(1024 * 1024)
    try:
        value = json.loads(raw) if raw else {}
    except Exception:
        value = {"parse_error": True}
    request = {
        "queued_utc": utils.utc_now(),
        "session_id": value.get("session_id") if isinstance(value, dict) else None,
        "transcript_path": value.get("transcript_path") if isinstance(value, dict) else None,
        "cwd": value.get("cwd") if isinstance(value, dict) else None,
        "reason": value.get("reason") if isinstance(value, dict) else None,
    }
    transcript = request.get("transcript_path")
    source = pathlib.Path(transcript).expanduser() if isinstance(transcript, str) and transcript else None
    if workspace_is_excluded(config, request.get("cwd")) or (
        source is not None and transcript_is_excluded(config, source)
    ):
        request["status"] = "excluded_workspace"
        return request
    if isinstance(transcript, str) and transcript:
        projects = pathlib.Path(config["sources"]["claude"]["root"]) / "projects"
        try:
            resolved = source.resolve(strict=True)
            projects_resolved = projects.resolve(strict=True)
            if resolved.is_file() and (resolved == projects_resolved or projects_resolved in resolved.parents):
                sid = request.get("session_id") or pathlib.Path(resolved).stem
                checkpoint = configuration.state_root(config) / "inbox" / "claude" / str(sid) / utils.capture_id()
                checkpoint.mkdir(parents=True, exist_ok=False, mode=0o700)
                warnings: List[str] = []
                storage.copy_clone(resolved, checkpoint / "transcript.jsonl", warnings)
                utils.write_json(checkpoint / "event.json", {**request, "warnings": warnings})
                request["micro_checkpoint"] = str(checkpoint)
        except Exception as exc:
            request["micro_checkpoint_error"] = repr(exc)
    destination = configuration.state_root(config) / "queue" / f"{utils.capture_id()}-{uuid.uuid4().hex}.json"
    utils.write_json(destination, request)
    return request


def power_eligible(
    config: Dict[str, Any],
    *,
    require_ac_power: Optional[bool] = None,
) -> Tuple[bool, str]:
    ac_required = (
        bool(config.get("require_ac_power", True))
        if require_ac_power is None
        else require_ac_power
    )
    battery = utils.run(["/usr/bin/pmset", "-g", "batt"], check=False)
    text = (battery.stdout or "") + (battery.stderr or "")
    on_ac_power = "AC Power" in text
    if ac_required and not on_ac_power:
        return False, "deferred: Mac is running on battery"
    current = utils.run(["/usr/bin/pmset", "-g"], check=False)
    settings = (current.stdout or "") + (current.stderr or "")
    if config.get("skip_low_power_mode", True) and re.search(r"^\s*powermode\s+1\s*$", settings, re.M):
        return False, "deferred: Low Power Mode is active"
    source = "AC power" if on_ac_power else "battery power permitted"
    return True, f"{source} and normal power mode"


def run_scheduled(config: Dict[str, Any]) -> Dict[str, Any]:
    eligible, power_reason = power_eligible(config)
    if not eligible:
        conn = catalog.connect_catalog(config)
        try:
            catalog.record_health(conn, "scheduled-capture", "deferred", power_reason)
            reports.render_status(config, conn)
        finally:
            conn.close()
        return {"status": "deferred", "reason": power_reason}
    queue = sorted((configuration.state_root(config) / "queue").glob("*.json"))
    reasons = []
    for path in queue:
        with contextlib.suppress(Exception):
            request = utils.read_json(path, {})
            reasons.append(f"{request.get('session_id') or 'unknown'}:{request.get('reason') or 'end'}")
    reason = "session-end:" + ",".join(reasons[:20]) if reasons else "daily-launchd"
    try:
        with storage.exclusive_lock(config, wait=False):
            result = capture.capture(config, reason, refresh_legacy_documents=False)
    except RuntimeError as exc:
        if "already running" in str(exc):
            return {"status": "coalesced", "reason": str(exc)}
        raise
    for path in queue:
        with contextlib.suppress(OSError):
            path.unlink()
    return result


def run_verify_scheduled(config: Dict[str, Any]) -> Dict[str, Any]:
    eligible, power_reason = power_eligible(
        config,
        require_ac_power=bool(config.get("verify_require_ac_power", True)),
    )
    if not eligible:
        conn = catalog.connect_catalog(config)
        try:
            catalog.record_health(conn, "scheduled-verify", "deferred", power_reason)
            reports.render_status(config, conn)
        finally:
            conn.close()
        return {"status": "deferred", "reason": power_reason}
    with storage.exclusive_lock(config, wait=False):
        return capture.verify_archive(config, read_data=True)
