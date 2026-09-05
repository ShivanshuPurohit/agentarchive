#!/usr/bin/env python3
"""Configure the five portable AgentArchive rollout folders in Syncthing.

This intentionally does not synchronize either complete harness home. Codex
SQLite/WAL, locks, IPC, credentials, and machine-local UI state belong in a
device-specific recovery backup, not in a live multi-writer folder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from privacy_filter import transcript_is_excluded  # noqa: E402


DEVICE_ID = re.compile(r"^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$")
SESSION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
IGNORE_START = "// agentarchive-private-workspaces:start"
IGNORE_END = "// agentarchive-private-workspaces:end"


def run(binary: str, arguments: List[str]) -> str:
    result = subprocess.run(
        [binary, *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown Syncthing error").strip()
        raise RuntimeError(f"Syncthing command failed: {detail}")
    return result.stdout


def cli(binary: str, arguments: List[str]) -> str:
    return run(binary, ["cli", *arguments])


def json_cli(binary: str, arguments: List[str]) -> Dict[str, Any]:
    value = json.loads(cli(binary, arguments))
    if not isinstance(value, dict):
        raise RuntimeError("Syncthing returned a non-object configuration value")
    return value


def folder_definitions(
    claude_home: pathlib.Path,
    codex_home: pathlib.Path,
    portable_state: Optional[pathlib.Path] = None,
    portable_role: str = "",
) -> List[Dict[str, str]]:
    folders = [
        {
            "id": "agentarchive-claude-projects",
            "label": "AgentArchive — Claude projects and tool results",
            "path": str(claude_home / "projects"),
            "type": "sendreceive",
        },
        {
            "id": "agentarchive-claude-file-history",
            "label": "AgentArchive — Claude file history",
            "path": str(claude_home / "file-history"),
            "type": "sendreceive",
        },
        {
            "id": "agentarchive-codex-sessions",
            "label": "AgentArchive — Codex sessions",
            "path": str(codex_home / "sessions"),
            "type": "sendreceive",
        },
        {
            "id": "agentarchive-codex-archived-sessions",
            "label": "AgentArchive — Codex archived sessions",
            "path": str(codex_home / "archived_sessions"),
            "type": "sendreceive",
        },
        {
            "id": "agentarchive-codex-attachments",
            "label": "AgentArchive — Codex attachments",
            "path": str(codex_home / "attachments"),
            "type": "sendreceive",
        },
    ]
    if portable_state is not None:
        folders.append(
            {
                "id": "agentarchive-portable-state",
                "label": "AgentArchive — consistent portable query state",
                "path": str(portable_state),
                "type": "sendonly" if portable_role == "source" else "receiveonly",
            }
        )
    return folders


def conflict_files(folders: List[Dict[str, str]]) -> List[str]:
    found: List[str] = []
    for folder in folders:
        root = pathlib.Path(folder["path"])
        if not root.is_dir() and folder["type"] == "receiveonly":
            continue
        if not root.is_dir():
            raise RuntimeError(f"rollout folder is unavailable: {root}")
        found.extend(
            f"{folder['id']}:{path.relative_to(root).as_posix()}"
            for path in root.rglob("*")
            if path.is_file() and ".sync-conflict-" in path.name
        )
    return sorted(found)


def privacy_patterns(
    config: Dict[str, Any], folders: List[Dict[str, str]]
) -> Dict[str, List[str]]:
    """Build local-only Syncthing ignores without exporting private metadata."""
    by_id = {folder["id"]: pathlib.Path(folder["path"]) for folder in folders}
    patterns: Dict[str, set[str]] = {folder_id: set() for folder_id in by_id}
    if "agentarchive-portable-state" in patterns:
        patterns["agentarchive-portable-state"].update(
            {
                "/catalog.sqlite-wal",
                "/catalog.sqlite-shm",
                "/catalog.sqlite-journal",
                "/repository-not-synced",
            }
        )
    session_ids: set[str] = set()
    claude_id = "agentarchive-claude-projects"
    claude_root = by_id[claude_id]
    for transcript in claude_root.rglob("*.jsonl"):
        if not transcript_is_excluded(config, transcript):
            continue
        relative = transcript.relative_to(claude_root)
        patterns[claude_id].add("/" + relative.parts[0])
        session_ids.update(SESSION_ID.findall(transcript.name))
    for folder_id in (
        "agentarchive-codex-sessions",
        "agentarchive-codex-archived-sessions",
    ):
        root = by_id[folder_id]
        for transcript in root.rglob("*.jsonl"):
            if not transcript_is_excluded(config, transcript):
                continue
            patterns[folder_id].add("/" + transcript.relative_to(root).as_posix())
            session_ids.update(SESSION_ID.findall(transcript.name))
    if session_ids:
        for folder_id, root in by_id.items():
            if folder_id == "agentarchive-portable-state" or not root.is_dir():
                continue
            for path in root.rglob("*"):
                relative = path.relative_to(root).as_posix()
                if any(session_id in relative for session_id in session_ids):
                    patterns[folder_id].add("/" + relative)
    result: Dict[str, List[str]] = {}
    for folder_id, values in patterns.items():
        unsafe = [value for value in values if any(char in value for char in "*?[]{}")]
        if unsafe:
            raise RuntimeError(
                f"privacy ignore path contains unsupported glob syntax in {folder_id}"
            )
        result[folder_id] = sorted(values)
    return result


def write_privacy_ignores(
    folders: List[Dict[str, str]], patterns: Dict[str, List[str]], backup: pathlib.Path
) -> None:
    for folder in folders:
        values = patterns.get(folder["id"], [])
        if not values:
            continue
        root = pathlib.Path(folder["path"])
        ignore = root / ".stignore"
        existing = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
        start = existing.find(IGNORE_START)
        end = existing.find(IGNORE_END)
        if (start < 0) != (end < 0) or (start >= 0 and end < start):
            raise RuntimeError(f"refusing malformed AgentArchive block in {ignore}")
        if start >= 0:
            end += len(IGNORE_END)
            existing = existing[:start].rstrip() + existing[end:].lstrip("\n")
        block = "\n".join((IGNORE_START, *values, IGNORE_END))
        updated = existing.rstrip() + ("\n\n" if existing.strip() else "") + block + "\n"
        backup.mkdir(parents=True, exist_ok=True)
        if ignore.is_file():
            shutil.copy2(ignore, backup / f"{folder['id']}.stignore")
        temporary = ignore.with_name(f".{ignore.name}.tmp-{os.getpid()}")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, ignore)


def configure(args: argparse.Namespace) -> Dict[str, Any]:
    binary = args.syncthing or shutil.which("syncthing")
    if not binary:
        raise RuntimeError("syncthing is not installed or is absent from PATH")
    if not args.acknowledge_complete_rollout_scope:
        raise RuntimeError(
            "refusing to expose rollout folders until "
            "--acknowledge-complete-rollout-scope is supplied"
        )
    peer_id = args.peer_device_id.upper()
    if not DEVICE_ID.fullmatch(peer_id):
        raise RuntimeError("peer device ID is not a valid Syncthing device ID")
    local_id = run(binary, ["device-id"]).strip().upper()
    if not DEVICE_ID.fullmatch(local_id):
        raise RuntimeError("local Syncthing device ID is unavailable")
    if local_id == peer_id:
        raise RuntimeError("peer device ID is the same as this device")

    portable = args.portable_state.resolve() if args.portable_state else None
    folders = folder_definitions(
        args.claude_home,
        args.codex_home,
        portable,
        args.portable_state_role,
    )
    if not args.agentarchive_config.is_file():
        raise RuntimeError(
            f"AgentArchive privacy configuration is unavailable: {args.agentarchive_config}"
        )
    privacy_config = json.loads(args.agentarchive_config.read_text(encoding="utf-8"))
    ignored = privacy_patterns(privacy_config, folders)
    conflicts = conflict_files(folders)
    if conflicts:
        raise RuntimeError(
            f"found {len(conflicts)} unresolved .sync-conflict files; first: {conflicts[0]}"
        )
    plan = {
        "local_device_id": local_id,
        "peer_device_id": peer_id,
        "peer_name": args.peer_name,
        "folders": folders,
        "folder_types": {folder["id"]: folder["type"] for folder in folders},
        "received_file_versioning": {
            "type": "simple",
            "keep": 999999,
            "cleanoutDays": 0,
        },
        "max_conflicts": -1,
        "ignore_permissions": True,
        "private_ignore_counts": {
            folder_id: len(values) for folder_id, values in ignored.items() if values
        },
        "one_writer_rule": "Do not append the same session UUID on both devices at once.",
    }
    if not args.apply:
        return {"status": "dry-run", **plan}

    backup_root = args.backup_dir
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"syncthing-config-before-agentarchive-{timestamp}.json"
    backup.write_text(cli(binary, ["config", "dump-json"]), encoding="utf-8")
    write_privacy_ignores(
        folders,
        ignored,
        backup_root / f"stignore-before-agentarchive-{timestamp}",
    )

    device_ids = set(filter(None, cli(binary, ["config", "devices", "list"]).splitlines()))
    if peer_id not in device_ids:
        peer = json_cli(binary, ["config", "defaults", "device", "dump-json"])
        peer.update(
            {
                "deviceID": peer_id,
                "name": args.peer_name,
                "addresses": ["dynamic"],
                "paused": False,
                "autoAcceptFolders": False,
                "untrusted": False,
            }
        )
        cli(binary, ["config", "devices", "add-json", json.dumps(peer)])

    existing_folders = set(
        filter(None, cli(binary, ["config", "folders", "list"]).splitlines())
    )
    for definition in folders:
        pathlib.Path(definition["path"]).mkdir(parents=True, exist_ok=True)
        if definition["id"] in existing_folders:
            folder = json_cli(
                binary,
                ["config", "folders", definition["id"], "dump-json"],
            )
        else:
            folder = json_cli(binary, ["config", "defaults", "folder", "dump-json"])
        folder.update(
            {
                "id": definition["id"],
                "label": definition["label"],
                "path": definition["path"],
                "type": definition["type"],
                "ignorePerms": True,
                "maxConflicts": -1,
                "blockIndexing": True,
                "paused": False,
                "versioning": {
                    "type": "simple",
                    "params": {"keep": "999999", "cleanoutDays": "0"},
                    "cleanupIntervalS": 3600,
                    "fsPath": "",
                    "fsType": "basic",
                },
            }
        )
        devices = {
            str(item.get("deviceID")): item
            for item in folder.get("devices", [])
            if isinstance(item, dict) and item.get("deviceID")
        }
        devices.setdefault(local_id, {"deviceID": local_id, "introducedBy": "", "encryptionPassword": ""})
        devices.setdefault(peer_id, {"deviceID": peer_id, "introducedBy": "", "encryptionPassword": ""})
        folder["devices"] = list(devices.values())
        cli(binary, ["config", "folders", "add-json", json.dumps(folder)])

    cli(binary, ["operations", "restart"])
    return {"status": "configured", "config_backup": str(backup), **plan}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer-device-id", required=True)
    parser.add_argument("--peer-name", required=True)
    parser.add_argument("--claude-home", type=pathlib.Path, default=pathlib.Path.home() / ".claude")
    parser.add_argument("--codex-home", type=pathlib.Path, default=pathlib.Path.home() / ".codex")
    parser.add_argument("--syncthing", default="")
    default_config = (
        pathlib.Path.home() / "Library/Application Support/AgentArchive/config.json"
        if sys.platform == "darwin"
        else pathlib.Path.home()
        / ".local/share/AgentArchiveImports/current/portable/config.wsl-readonly.json"
    )
    parser.add_argument("--agentarchive-config", type=pathlib.Path, default=default_config)
    parser.add_argument("--portable-state", type=pathlib.Path)
    parser.add_argument(
        "--portable-state-role",
        choices=("source", "receiver"),
        default="source",
    )
    parser.add_argument(
        "--backup-dir",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".agentarchive-sync",
    )
    parser.add_argument("--acknowledge-complete-rollout-scope", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    args.claude_home = args.claude_home.expanduser().resolve()
    args.codex_home = args.codex_home.expanduser().resolve()
    if args.portable_state:
        args.portable_state = args.portable_state.expanduser().resolve()
    args.agentarchive_config = args.agentarchive_config.expanduser().resolve()
    args.backup_dir = args.backup_dir.expanduser().resolve()
    try:
        result = configure(args)
    except Exception as exc:
        print(f"configure-syncthing: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
