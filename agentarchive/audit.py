"""Read-only accounting of abandoned staging trees against durable snapshots."""

from __future__ import annotations

import collections
import gzip
import hashlib
import json
import os
import pathlib
import sqlite3
import stat
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import common as utils, config as configuration, metadata, storage
from .common import DURABLE_CAPTURE_STATUSES


def archived_file_indexes(
    conn: sqlite3.Connection,
    snapshot_ids: set[str],
    manifest_root: Optional[pathlib.Path] = None,
) -> Tuple[
    Dict[Tuple[str, str], List[Dict[str, Any]]],
    Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]],
]:
    placeholders = ",".join("?" for _ in DURABLE_CAPTURE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT f.*,c.restic_snapshot,c.status
        FROM files f JOIN captures c USING(capture_id)
        WHERE c.status IN ({placeholders})
          AND c.restic_snapshot IS NOT NULL
          AND f.change_kind != 'deleted'
        """,
        DURABLE_CAPTURE_STATUSES,
    )
    by_path: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
    by_content: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]] = collections.defaultdict(list)
    def include(value: Dict[str, Any]) -> None:
        if str(value["restic_snapshot"]) not in snapshot_ids:
            return
        by_path[(str(value["harness"]), str(value["relpath"]))].append(value)
        by_content[(str(value["file_type"]), value.get("sha256"))].append(value)
    for row in rows:
        include(dict(row))
    # Delta-only catalog inventories must still prove that an unchanged file
    # exists in a retained snapshot after its original snapshot is pruned.
    if manifest_root is not None:
        captures = conn.execute(
            f"SELECT capture_id,restic_snapshot,status,manifest_sha256 FROM captures "
            f"WHERE status IN ({placeholders})", DURABLE_CAPTURE_STATUSES,
        )
        for capture in captures:
            if capture["restic_snapshot"] not in snapshot_ids:
                continue
            path = manifest_root / f"{capture['capture_id']}.jsonl.gz"
            if not path.is_file():
                continue
            if not capture["manifest_sha256"] or utils.sha256_file(path) != capture["manifest_sha256"]:
                raise RuntimeError(f"capture manifest checksum mismatch: {path}")
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    value = json.loads(line)
                    if value.get("change_kind") == "unchanged":
                        include({**value, "capture_id": capture["capture_id"],
                                 "restic_snapshot": capture["restic_snapshot"], "status": capture["status"]})
    return by_path, by_content


def physical_entry_fingerprint(
    file_rows: Sequence[Dict[str, Any]],
    directory_rows: Sequence[Dict[str, Any]],
) -> str:
    fields = (
        "harness",
        "relpath",
        "file_type",
        "size_bytes",
        "mtime_ns",
        "birthtime_ns",
        "mode",
        "uid",
        "gid",
        "flags",
        "sha256",
        "link_target",
        "xattrs",
        "empty",
    )
    physical = [
        {field: row.get(field) for field in fields}
        for row in list(directory_rows) + list(file_rows)
    ]
    physical.sort(key=lambda row: (str(row.get("harness")), str(row.get("relpath"))))
    encoded = json.dumps(
        physical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audit_incomplete_stage(
    stage: pathlib.Path,
    by_path: Dict[Tuple[str, str], List[Dict[str, Any]]],
    by_content: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    data = stage / "data"
    if not data.is_dir():
        raise RuntimeError(f"staging tree has no data directory: {stage}")
    file_rows: List[Dict[str, Any]] = []
    directory_rows: List[Dict[str, Any]] = []
    counts: collections.Counter[str] = collections.Counter()
    byte_counts: collections.Counter[str] = collections.Counter()
    warnings: List[str] = []
    started = time.monotonic()
    for path in sorted(data.rglob("*")):
        info = path.lstat()
        parts = path.relative_to(data).parts
        if not parts:
            continue
        harness = parts[0]
        relative = pathlib.PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else "."
        mode = stat.S_IMODE(info.st_mode)
        birthtime = getattr(info, "st_birthtime", None)
        common: Dict[str, Any] = {
            "capture_id": stage.name,
            "harness": harness,
            "relpath": relative,
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "birthtime_ns": int(birthtime * 1_000_000_000) if birthtime is not None else None,
            "mode": mode,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "flags": getattr(info, "st_flags", None),
        }
        xattrs, xattr_error = metadata.path_xattrs(path)
        common["xattrs"] = xattrs
        if xattr_error:
            warnings.append(f"xattr inventory failed for {path}: {xattr_error}")
        if stat.S_ISDIR(info.st_mode):
            try:
                empty = next(path.iterdir(), None) is None
            except OSError as exc:
                raise RuntimeError(f"cannot inspect staging directory {path}: {exc!r}")
            directory_rows.append(
                {
                    **common,
                    "file_type": "directory",
                    "sha256": None,
                    "link_target": None,
                    "empty": empty,
                }
            )
            counts["directories"] += 1
            if empty:
                counts["empty_directories"] += 1
            continue
        if stat.S_ISLNK(info.st_mode):
            file_type = "symlink"
            link_target = os.readlink(str(path))
            encoded_target = link_target.encode("utf-8", errors="surrogateescape")
            size = len(encoded_target)
            digest = hashlib.sha256(b"symlink\0" + encoded_target).hexdigest()
        elif stat.S_ISREG(info.st_mode):
            file_type = "file"
            link_target = None
            size = info.st_size
            digest = utils.sha256_file(path)
        else:
            file_type = "other"
            link_target = None
            size = info.st_size
            digest = None
            warnings.append(f"unsupported special entry requires manual inspection: {path}")
        candidates = by_path.get((harness, relative), [])
        exact = [
            row
            for row in candidates
            if row["file_type"] == file_type and row["sha256"] == digest
        ]
        content = by_content.get((file_type, digest), [])
        if exact:
            coverage = "same_path_archived"
            metadata_exact = any(
                row["size_bytes"] == size
                and row["mtime_ns"] == info.st_mtime_ns
                and row["mode"] == mode
                and row["uid"] == info.st_uid
                and row["gid"] == info.st_gid
                and row["link_target"] == link_target
                for row in exact
            )
            counts[
                "same_path_metadata_exact"
                if metadata_exact
                else "same_path_metadata_differs"
            ] += 1
        elif content:
            coverage = "content_elsewhere_archived"
        else:
            coverage = "not_in_durable_snapshot"
        row = {
            **common,
            "file_type": file_type,
            "size_bytes": size,
            "sha256": digest,
            "link_target": link_target,
            "change_kind": coverage,
        }
        file_rows.append(row)
        counts[file_type] += 1
        counts[coverage] += 1
        counts["file_entries"] += 1
        byte_counts[coverage] += size
        byte_counts["file_entries"] += size
    counts["all_entries"] = counts["file_entries"] + counts["directories"]
    fingerprint = physical_entry_fingerprint(file_rows, directory_rows)
    return {
        "schema_version": 1,
        "stage_id": stage.name,
        "stage_path": str(stage),
        "generated_utc": utils.utc_now(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "data_fingerprint": fingerprint,
        "counts": dict(counts),
        "bytes": dict(byte_counts),
        "warnings": warnings,
        "files": file_rows,
        "directories": directory_rows,
    }


def write_staging_audit(config: Dict[str, Any], audit: Dict[str, Any]) -> pathlib.Path:
    destination = configuration.state_root(config) / "audits" / "staging" / f"{audit['stage_id']}.audit.json.gz"
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True).encode("utf-8")
    utils.atomic_write(destination, gzip.compress(encoded, compresslevel=6, mtime=0), 0o600)
    return destination


def summarize_staging_audit(audit: Dict[str, Any], destination: pathlib.Path) -> Dict[str, Any]:
    counts = audit["counts"]
    sizes = audit["bytes"]
    return {
        "stage_id": audit["stage_id"],
        "stage_path": audit["stage_path"],
        "audit_path": str(destination),
        "data_fingerprint": audit["data_fingerprint"],
        "file_entries": counts.get("file_entries", 0),
        "directories": counts.get("directories", 0),
        "empty_directories": counts.get("empty_directories", 0),
        "same_path_archived": counts.get("same_path_archived", 0),
        "content_elsewhere_archived": counts.get("content_elsewhere_archived", 0),
        "not_in_durable_snapshot": counts.get("not_in_durable_snapshot", 0),
        "logical_bytes": sizes.get("file_entries", 0),
        "not_in_durable_snapshot_bytes": sizes.get("not_in_durable_snapshot", 0),
        "warnings": audit.get("warnings") or [],
    }


def staging_candidates(config: Dict[str, Any], conn: sqlite3.Connection) -> List[pathlib.Path]:
    staging = configuration.state_root(config) / "staging"
    if not staging.is_dir():
        return []
    candidates: List[pathlib.Path] = []
    for stage in sorted(path for path in staging.iterdir() if path.is_dir()):
        row = conn.execute(
            "SELECT status FROM captures WHERE capture_id=?",
            (stage.name,),
        ).fetchone()
        # Callers hold the archive lock, so a "capturing" row here is stale
        # state left by a killed process rather than an active capture.
        if row is None or row["status"] in (
            "failed",
            "capturing",
            "orphan_recovery_pending_cleanup",
        ):
            candidates.append(stage)
    return candidates


def audit_staging(config: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    snapshot_ids = storage.known_restic_snapshot_ids(config)
    by_path, by_content = archived_file_indexes(conn, snapshot_ids, configuration.state_root(config) / "manifests")
    audits: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for stage in staging_candidates(config, conn):
        audit = audit_incomplete_stage(stage, by_path, by_content)
        destination = write_staging_audit(config, audit)
        audits.append(audit)
        summaries.append(summarize_staging_audit(audit, destination))
    unarchived_contents: Dict[Tuple[str, Optional[str]], int] = {}
    for audit in audits:
        for row in audit["files"]:
            if row["change_kind"] != "not_in_durable_snapshot":
                continue
            unarchived_contents.setdefault(
                (str(row["file_type"]), row.get("sha256")),
                int(row["size_bytes"]),
            )
    result = {
        "generated_utc": utils.utc_now(),
        "candidate_stages": len(summaries),
        "stages": summaries,
        "totals": {
            "file_entries": sum(item["file_entries"] for item in summaries),
            "directories": sum(item["directories"] for item in summaries),
            "logical_bytes": sum(item["logical_bytes"] for item in summaries),
            "not_in_durable_snapshot": sum(
                item["not_in_durable_snapshot"] for item in summaries
            ),
            "not_in_durable_snapshot_bytes": sum(
                item["not_in_durable_snapshot_bytes"] for item in summaries
            ),
            "not_in_durable_snapshot_distinct_contents": len(unarchived_contents),
            "not_in_durable_snapshot_distinct_content_bytes": sum(
                unarchived_contents.values()
            ),
        },
    }
    utils.write_json(configuration.state_root(config) / "staging-audit.json", result)
    result["_full_audits"] = audits
    result["_snapshot_ids"] = snapshot_ids
    result["_by_path"] = by_path
    result["_by_content"] = by_content
    return result


def public_staging_audit(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}
