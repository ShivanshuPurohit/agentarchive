"""Filtered source staging, incremental file inventories, and manifests."""

from __future__ import annotations

import collections
import contextlib
import gzip
import json
import os
import pathlib
import shutil
import sqlite3
import stat
from privacy_filter import transcript_is_excluded
from typing import Any, Dict, List, Sequence, Tuple

from . import common as utils, config as configuration, indexing, metadata, storage
from .common import HOME, VERSION


def stage_sources(config: Dict[str, Any], stage: pathlib.Path) -> List[str]:
    warnings: List[str] = []
    data = stage / "data"
    for harness, definition in config["sources"].items():
        root = pathlib.Path(definition["root"]).expanduser()
        target_root = data / harness
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for relative in definition.get("paths", []):
            source = root / relative
            if not source.exists() and not source.is_symlink():
                continue
            storage.copy_clone(source, target_root / relative, warnings)
        for relative in definition.get("sqlite", []):
            source = root / relative
            if not source.is_file():
                continue
            destination = target_root / "sqlite_consistent" / relative
            ok, detail = storage.sqlite_backup(source, destination)
            if not ok:
                warnings.append(f"SQLite backup failed for {source}: {detail}")
    inbox = configuration.state_root(config) / "inbox"
    if inbox.is_dir() and any(inbox.iterdir()):
        storage.copy_clone(inbox, data / "hook_inbox", warnings)
    excluded = collections.Counter()
    for harness, path, _ in list(indexing.indexable_jsonl(stage)):
        if not transcript_is_excluded(config, path):
            continue
        hook_root = data / "hook_inbox"
        if hook_root in path.parents:
            shutil.rmtree(path.parent)
        else:
            path.unlink()
        excluded[harness] += 1
    if excluded:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(excluded.items()))
        warnings.append(f"Excluded private-workspace transcripts: {detail}")
    sync_conflicts = sorted(
        path.relative_to(data).as_posix()
        for path in data.rglob("*")
        if path.is_file() and ".sync-conflict-" in path.name
    )
    if sync_conflicts:
        warnings.append(
            f"Preserved {len(sync_conflicts)} Syncthing conflict files without indexing "
            f"them; first conflict: {sync_conflicts[0]}"
        )
    # Keep the archiver itself recoverable without recursively backing up its
    # repository or operational database.  None of these files contains the
    # Keychain-held restic password.
    system_root = data / "agentarchive"
    capture_label, verify_label = configuration.launchd_labels(config)
    system_sources = [
        ("agent-archive", configuration.state_root(config) / "bin" / "agent-archive"),
        ("reasoning_index.py", configuration.state_root(config) / "bin" / "reasoning_index.py"),
        ("privacy_filter.py", configuration.state_root(config) / "bin" / "privacy_filter.py"),
        ("transcript_model.py", configuration.state_root(config) / "bin" / "transcript_model.py"),
        ("transcript_viewer.py", configuration.state_root(config) / "bin" / "transcript_viewer.py"),
        ("transcript_media.py", configuration.state_root(config) / "bin" / "transcript_media.py"),
        ("transcript_usage.py", configuration.state_root(config) / "bin" / "transcript_usage.py"),
        ("viewer", configuration.state_root(config) / "bin" / "viewer"),
        ("README.md", configuration.state_root(config) / "README.md"),
        ("START_HERE.md", configuration.state_root(config) / "START_HERE.md"),
        ("STATUS.md", configuration.state_root(config) / "STATUS.md"),
        ("dedup-report.json", configuration.state_root(config) / "dedup-report.json"),
        ("dedup-result.json", configuration.state_root(config) / "dedup-result.json"),
        (
            "metadata-supplements",
            configuration.state_root(config) / "metadata-supplements",
        ),
        ("config.json", pathlib.Path(config.get("config_path") or configuration.state_root(config) / "config.json")),
        ("launchd/capture.plist", HOME / "Library" / "LaunchAgents" / f"{capture_label}.plist"),
        ("launchd/verify.plist", HOME / "Library" / "LaunchAgents" / f"{verify_label}.plist"),
    ]
    system_sources.extend(
        (f"agentarchive/{path.name}", path)
        for path in sorted((configuration.state_root(config) / "bin/agentarchive").glob("*.py"))
    )
    for relative, source in system_sources:
        if source.is_file() or source.is_dir():
            storage.copy_clone(source, system_root / relative, warnings)
    return warnings


def pending_inbox_items(config: Dict[str, Any]) -> List[pathlib.Path]:
    root = configuration.state_root(config) / "inbox" / "claude"
    if not root.is_dir():
        return []
    result: List[pathlib.Path] = []
    for session in root.iterdir():
        if not session.is_dir():
            continue
        result.extend(path for path in session.iterdir() if path.is_dir())
    return result


def remove_captured_inbox(items: Sequence[pathlib.Path]) -> None:
    for path in items:
        with contextlib.suppress(OSError):
            shutil.rmtree(path)
        parent = path.parent
        with contextlib.suppress(OSError):
            parent.rmdir()


def prior_files(conn: sqlite3.Connection) -> Dict[Tuple[str, str], sqlite3.Row]:
    return {
        (str(row["harness"]), str(row["relpath"])): row
        for row in conn.execute("SELECT * FROM latest_files WHERE present=1")
    }


def inventory_stage(
    conn: sqlite3.Connection,
    stage: pathlib.Path,
    current_capture: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    previous = prior_files(conn)
    rows: List[Dict[str, Any]] = []
    counters = collections.Counter()
    data = stage / "data"
    current_keys = set()
    for harness_root in sorted(p for p in data.iterdir() if p.is_dir()):
        harness = harness_root.name
        for path in sorted(harness_root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(harness_root).as_posix()
            key = (harness, relative)
            current_keys.add(key)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                file_type = "symlink"
                link_target = os.readlink(str(path))
                size = len(link_target.encode("utf-8", errors="surrogateescape"))
                digest = utils.sha256_text("symlink\0" + link_target)
            elif stat.S_ISREG(info.st_mode):
                file_type = "file"
                link_target = None
                size = info.st_size
                old = previous.get(key)
                if (
                    old is not None
                    and old["size_bytes"] == size
                    and old["mtime_ns"] == info.st_mtime_ns
                    and old["mode"] == stat.S_IMODE(info.st_mode)
                    and old["sha256"]
                ):
                    digest = str(old["sha256"])
                else:
                    digest = utils.sha256_file(path)
            else:
                file_type = "other"
                link_target = None
                size = info.st_size
                digest = None
            old = previous.get(key)
            if old is None:
                change = "new"
            elif digest != old["sha256"] or file_type != old["file_type"]:
                change = "changed"
            else:
                change = "unchanged"
            counters[change] += 1
            counters["files"] += 1
            counters["bytes"] += size
            rows.append(
                {
                    "capture_id": current_capture,
                    "harness": harness,
                    "relpath": relative,
                    "file_type": file_type,
                    "size_bytes": size,
                    "mtime_ns": info.st_mtime_ns,
                    "mode": stat.S_IMODE(info.st_mode),
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "sha256": digest,
                    "link_target": link_target,
                    "change_kind": change,
                }
            )
    for (harness, relative), old in sorted(previous.items()):
        if (harness, relative) in current_keys:
            continue
        counters["deleted"] += 1
        rows.append(
            {
                "capture_id": current_capture,
                "harness": harness,
                "relpath": relative,
                "file_type": str(old["file_type"]),
                "size_bytes": old["size_bytes"],
                "mtime_ns": old["mtime_ns"],
                "mode": old["mode"],
                "uid": old["uid"],
                "gid": old["gid"],
                "sha256": old["sha256"],
                "link_target": old["link_target"],
                "change_kind": "deleted",
            }
        )
    return rows, dict(counters)


def persist_inventory(
    conn: sqlite3.Connection,
    current_capture: str,
    rows: List[Dict[str, Any]],
) -> None:
    fields = (
        "capture_id",
        "harness",
        "relpath",
        "file_type",
        "size_bytes",
        "mtime_ns",
        "mode",
        "uid",
        "gid",
        "sha256",
        "link_target",
        "change_kind",
    )
    conn.executemany(
        f"INSERT INTO files({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        [[row.get(field) for field in fields] for row in rows if row["change_kind"] != "unchanged"],
    )
    for row in rows:
        # Complete per-capture inventories already live in the checksummed
        # compressed manifest. SQLite only needs changes and current state.
        if row["change_kind"] == "unchanged":
            continue
        if row["change_kind"] == "deleted":
            conn.execute(
                "UPDATE latest_files SET present=0,last_capture=? WHERE harness=? AND relpath=?",
                (current_capture, row["harness"], row["relpath"]),
            )
            continue
        conn.execute(
            """
            INSERT INTO latest_files(
                harness,relpath,file_type,size_bytes,mtime_ns,mode,uid,gid,sha256,
                link_target,present,first_capture,last_capture
            ) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
            ON CONFLICT(harness,relpath) DO UPDATE SET
                file_type=excluded.file_type,size_bytes=excluded.size_bytes,
                mtime_ns=excluded.mtime_ns,mode=excluded.mode,uid=excluded.uid,
                gid=excluded.gid,sha256=excluded.sha256,link_target=excluded.link_target,
                present=1,last_capture=excluded.last_capture
            """,
            (
                row["harness"],
                row["relpath"],
                row["file_type"],
                row["size_bytes"],
                row["mtime_ns"],
                row["mode"],
                row["uid"],
                row["gid"],
                row["sha256"],
                row["link_target"],
                current_capture,
                current_capture,
            ),
        )


def write_manifest(
    config: Dict[str, Any],
    stage: pathlib.Path,
    current_capture: str,
    reason: str,
    rows: List[Dict[str, Any]],
    counters: Dict[str, int],
    warnings: List[str],
) -> Tuple[pathlib.Path, str]:
    meta = stage / "meta"
    meta.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = meta / "manifest.jsonl.gz"
    with gzip.open(str(manifest), "wt", encoding="utf-8", compresslevel=6) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    os.chmod(manifest, 0o600)
    digest = utils.sha256_file(manifest)
    platform_metadata, platform_digest, platform_entries, platform_warnings = (
        metadata.write_platform_metadata(stage)
    )
    warnings.extend(platform_warnings)
    capture_meta = {
        "schema_version": 1,
        "capture_id": current_capture,
        "created_utc": utils.utc_now(),
        "reason": reason,
        "agentarchive_version": VERSION,
        "manifest_sha256": digest,
        "platform_metadata_path": platform_metadata.name,
        "platform_metadata_sha256": platform_digest,
        "platform_metadata_entries": platform_entries,
        "counts": counters,
        "warnings": warnings,
        "sources": {
            name: {
                "root": definition["root"],
                "selected_paths": definition.get("paths", []),
                "consistent_sqlite": definition.get("sqlite", []),
                "excluded_classes": definition.get("excluded", []),
            }
            for name, definition in config["sources"].items()
        },
    }
    utils.write_json(meta / "capture.json", capture_meta)
    utils.write_json(
        meta / "restore-sentinel.json",
        {
            "capture_id": current_capture,
            "manifest_sha256": digest,
            "platform_metadata_path": platform_metadata.name,
            "platform_metadata_sha256": platform_digest,
            "created_utc": capture_meta["created_utc"],
        },
    )
    stable_manifest = configuration.state_root(config) / "manifests" / f"{current_capture}.jsonl.gz"
    shutil.copy2(manifest, stable_manifest)
    os.chmod(stable_manifest, 0o600)
    return manifest, digest
