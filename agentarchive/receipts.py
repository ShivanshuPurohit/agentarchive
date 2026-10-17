"""Receipts of transcripts that left the disk, read back from the restic snapshots still holding them."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from privacy_filter import transcript_is_excluded
from transcript_usage import UsageIndex

from . import storage
from .config import state_root
from .indexing import TRANSCRIPT_ROOTS, USAGE_VERSION, extract_event, write_receipts

CAPTURE_TAG = "capture-"


def live_snapshots(config: Dict[str, Any]) -> Dict[str, str]:
    """Capture id -> snapshot id, for every capture whose snapshot the repository still holds."""
    rows = json.loads(storage.restic(config, ["snapshots", "--json", "--tag", "agentarchive"]).stdout or "[]")
    return {
        tag[len(CAPTURE_TAG):]: row["id"]
        for row in rows
        for tag in row.get("tags") or []
        if tag.startswith(CAPTURE_TAG)
    }


def inventory_folder(harness: str, relpath: str) -> Optional[str]:
    """The `data` subfolder the inventory files an indexed transcript under."""
    for root_harness, folder, subfolder in TRANSCRIPT_ROOTS:
        if harness == root_harness and relpath.startswith(subfolder + "/"):
            return folder
    return None


def plan(conn: sqlite3.Connection, live: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Each transcript without receipts: the newest version a live snapshot holds, or that none does.

    A capture backs the stage up from inside it, so a snapshot holds `/data/<folder>/<relpath>`.
    """
    held: List[Dict[str, Any]] = []
    lost: List[Tuple[str, str]] = []
    pending = conn.execute(
        "SELECT harness, relpath FROM indexed_files WHERE usage_version < ? ORDER BY harness, relpath",
        (USAGE_VERSION,),
    ).fetchall()
    for harness, relpath in pending:
        folder = inventory_folder(harness, relpath)
        versions = conn.execute(
            "SELECT f.capture_id, f.size_bytes FROM files f JOIN captures c USING(capture_id) "
            "WHERE f.harness=? AND f.relpath=? AND f.sha256 IS NOT NULL "
            "AND f.change_kind NOT IN ('deleted', 'not_in_durable_snapshot') ORDER BY c.started_utc",
            (folder, relpath),
        ).fetchall() if folder else []
        kept = [(capture, size) for capture, size in versions if capture in live]
        if not kept:
            lost.append((harness, relpath))
            continue
        capture, size = kept[-1]
        held.append({
            "harness": harness, "relpath": relpath, "capture_id": capture, "snapshot": live[capture],
            "size_bytes": int(size or 0), "path": f"/data/{folder}/{relpath}",
        })
    return held, lost


def restore(config: Dict[str, Any], snapshot: str, paths: List[str], target: pathlib.Path) -> None:
    """Restore the named files of one snapshot under `target`, each at its snapshot path."""
    includes = target / "include.txt"
    includes.write_text("".join(path + "\n" for path in paths), encoding="utf-8")
    storage.restic(config, ["restore", snapshot, "--target", str(target), "--include-file", str(includes)])


def read_receipts(harness: str, relative: str, path: pathlib.Path) -> Tuple[UsageIndex, Optional[str]]:
    """One transcript's receipts and the session they belong to, read as the indexer reads a live file."""
    usage = UsageIndex()
    session_id = None
    last_model = None
    with path.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            session_id, _parent, _cwd, model, _blocks, _metadata = extract_event(harness, relative, obj)
            last_model = model or last_model
            usage.consume(harness, obj, line_no, [], last_model)
    return usage, session_id


def backfill(config: Dict[str, Any], conn: sqlite3.Connection, dry_run: bool = False) -> Dict[str, Any]:
    """Read receipts back for every transcript indexed before the usage table existed and gone since.

    A transcript whose every version was in a snapshot since forgotten stays without receipts and is
    counted as unrecoverable; the catalog keeps saying it carries no tokens.
    """
    held, lost = plan(conn, live_snapshots(config))
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "restorable": len(held),
        "unrecoverable": len(lost),
        "snapshots": len({item["snapshot"] for item in held}),
        "bytes": sum(item["size_bytes"] for item in held),
        "files_backfilled": 0,
        "model_calls_added": 0,
        "excluded": 0,
        "missing": 0,
    }
    if dry_run:
        return result
    by_snapshot: Dict[str, List[Dict[str, Any]]] = {}
    for item in held:
        by_snapshot.setdefault(item["snapshot"], []).append(item)
    for snapshot, items in by_snapshot.items():
        with tempfile.TemporaryDirectory(prefix="receipts-", dir=str(state_root(config))) as scratch:
            restore(config, snapshot, [item["path"] for item in items], pathlib.Path(scratch))
            with conn:
                for item in items:
                    restored = pathlib.Path(scratch) / item["path"].lstrip("/")
                    if not restored.is_file():
                        result["missing"] += 1
                        continue
                    key = (item["harness"], item["relpath"])
                    if transcript_is_excluded(config, restored):
                        result["excluded"] += 1
                    else:
                        usage, session_id = read_receipts(item["harness"], item["relpath"], restored)
                        result["model_calls_added"] += write_receipts(conn, *key, session_id, usage, True)
                        result["files_backfilled"] += 1
                    conn.execute(
                        "UPDATE indexed_files SET usage_version=? WHERE harness=? AND relpath=?",
                        (USAGE_VERSION, *key),
                    )
    return result
