#!/usr/bin/env python3
"""Publish a consistent plaintext AgentArchive query surface for another device."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from typing import Dict


SOURCE_ROOT = pathlib.Path(__file__).resolve().parent
if SOURCE_ROOT.name in ("scripts", "agentarchive"):
    SOURCE_ROOT = SOURCE_ROOT.parent
sys.path.insert(0, str(SOURCE_ROOT))
from agentarchive.runtime import copy_runtime


def atomic_json(path: pathlib.Path, value: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def backup_catalog(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(str(destination), timeout=60)
    try:
        src.backup(dst, pages=8192, sleep=0.05)
        journal_mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise RuntimeError(f"portable catalog journal mode remained {journal_mode!r}")
        result = dst.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"portable catalog quick_check returned {result!r}")
    finally:
        dst.close()
        src.close()


def copy_tree_if_present(source: pathlib.Path, destination: pathlib.Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        type=pathlib.Path,
        default=pathlib.Path.home() / "Library/Application Support/AgentArchive",
    )
    parser.add_argument(
        "--target",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".local/share/AgentArchivePortable",
    )
    parser.add_argument(
        "--source-root",
        type=pathlib.Path,
        default=SOURCE_ROOT,
    )
    args = parser.parse_args()
    state = args.state_root.expanduser().resolve()
    target = args.target.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    catalog = state / "catalog.sqlite"
    if not catalog.is_file():
        parser.error(f"AgentArchive catalog is unavailable: {catalog}")
    if target == pathlib.Path(target.anchor) or target == state:
        parser.error("portable target must be a distinct non-root directory")

    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".agentarchive-portable-",
        dir=str(target.parent),
    ) as temporary_text:
        staged_catalog = pathlib.Path(temporary_text) / "catalog.sqlite"
        backup_catalog(catalog, staged_catalog)
        os.chmod(staged_catalog, 0o600)
        os.replace(staged_catalog, target / "catalog.sqlite")

    for name in ("START_HERE.md", "STATUS.md"):
        source = state / name
        if source.is_file():
            shutil.copy2(source, target / name)
    copy_tree_if_present(state / "manifests", target / "manifests")
    copy_tree_if_present(state / "indexes", target / "indexes")
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    copy_runtime(source_root, bin_dir)

    portable_config: Dict[str, object] = {
        "schema_version": 1,
        "state_root": "~/.local/share/AgentArchivePortable",
        "restic_binary": "restic",
        "local_repository": "~/.local/share/AgentArchivePortable/repository-not-synced",
        "password_command": "false",
        "excluded_workspaces": [],
        "sources": {
            "claude": {"root": "~/.claude", "legacy_cutoff_utc": ""},
            "codex": {"root": "~/.codex", "legacy_cutoff_utc": ""},
        },
        "legacy_indexes": [],
        "offsite": {"enabled": False, "repository": "", "password_command": "", "environment": {}},
    }
    atomic_json(target / "config.portable.json", portable_config)
    receipt: Dict[str, object] = {
        "status": "complete",
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_catalog": str(catalog),
        "catalog_bytes": (target / "catalog.sqlite").stat().st_size,
        "target": str(target),
        "consumer": (
            "Run bin/agent-archive --config config.portable.json search QUERY "
            "inside WSL after Syncthing reports the folder up to date."
        ),
    }
    atomic_json(target / "export-receipt.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"export-portable-state: {exc}", file=sys.stderr)
        raise SystemExit(1)
