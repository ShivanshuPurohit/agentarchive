"""Capture and verification workflows consumed by the archive CLI."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import reasoning_index
import shutil
from typing import Any, Dict

from . import catalog, common as utils, config as configuration, indexing, reports, staging, storage
from .common import SUCCESSFUL_CAPTURE_STATUSES


def capture(
    config: Dict[str, Any],
    reason: str,
    refresh_legacy_documents: bool = True,
    *,
    check_repository: bool = True,
) -> Dict[str, Any]:
    state = configuration.state_root(config)
    utils.progress("capture: preparing current live sources (prior staging trees are not scanned)")
    utils.ensure_private_tree(state)
    storage.ensure_repository(config)
    conn = catalog.connect_catalog(config)
    try:
        purged_sessions = indexing.purge_excluded_sessions(config, conn)
    except Exception:
        conn.close()
        raise
    current_capture = utils.capture_id()
    stage = state / "staging" / current_capture
    stage.mkdir(parents=True, exist_ok=False, mode=0o700)
    inbox_items = staging.pending_inbox_items(config)
    started = utils.utc_now()
    conn.execute(
        "INSERT INTO captures(capture_id,started_utc,reason,status) VALUES(?,?,?,'capturing')",
        (current_capture, started, reason),
    )
    conn.commit()
    try:
        warnings = staging.stage_sources(config, stage)
        utils.progress("capture: APFS staging complete; inventorying changed files")
        if purged_sessions:
            warnings.append(f"Purged excluded sessions from derived indexes: {purged_sessions}")
        prior_stages = sorted(
            path.name
            for path in (state / "staging").iterdir()
            if path.is_dir() and path != stage
        )
        if prior_stages:
            warnings.append(
                f"Retained {len(prior_stages)} prior staging trees for explicit "
                f"staging-cleanup; normal capture did not scan them"
            )
        rows, counters = staging.inventory_stage(conn, stage, current_capture)
        staging.persist_inventory(conn, current_capture, rows)
        manifest, manifest_digest = staging.write_manifest(
            config, stage, current_capture, reason, rows, counters, warnings
        )
        conn.execute(
            """
            UPDATE captures SET file_count=?,logical_bytes=?,new_files=?,changed_files=?,
                unchanged_files=?,deleted_files=?,manifest_sha256=?,warnings_json=?
            WHERE capture_id=?
            """,
            (
                counters.get("files", 0),
                counters.get("bytes", 0),
                counters.get("new", 0),
                counters.get("changed", 0),
                counters.get("unchanged", 0),
                counters.get("deleted", 0),
                manifest_digest,
                json.dumps(warnings),
                current_capture,
            ),
        )
        conn.commit()
        utils.progress("capture: indexing new records and backfilling tool calls from older extractors")
        index_counts = indexing.index_capture(config, conn, stage, current_capture, rows)
        utils.progress(
            f"capture: indexed {index_counts['tool_calls_added']:,} new tool calls; "
            f"backfilled {index_counts['files_backfilled']:,} previously scanned files"
        )
        reasoning_counts = reasoning_index.index_stage(conn, stage, current_capture)
        if refresh_legacy_documents:
            reports.index_legacy_documents(config, conn)
        reports.render_start_here(config, conn)
        utils.progress("capture: incremental indexes complete; writing consistent catalog snapshot")
        catalog.backup_catalog_to_stage(conn, stage)
        utils.progress("capture: writing deduplicated local recovery snapshot")
        local_snapshot = storage.backup_stage(config, stage, current_capture)
        offsite_snapshot = None
        status = "complete"
        offsite = config.get("offsite") or {}
        if offsite.get("enabled"):
            try:
                storage.ensure_repository(config, offsite=True)
                offsite_snapshot = storage.backup_stage(config, stage, current_capture, offsite=True)
            except Exception as exc:
                warnings.append(f"offsite backup failed: {exc!r}")
                status = "local_complete_offsite_failed"
        conn.execute(
            """
            UPDATE captures SET finished_utc=?,status=?,restic_snapshot=?,offsite_snapshot=?,warnings_json=?
            WHERE capture_id=?
            """,
            (
                utils.utc_now(),
                status,
                local_snapshot,
                offsite_snapshot,
                json.dumps(warnings),
                current_capture,
            ),
        )
        conn.commit()
        # Refresh the live entry point only after the snapshot is durable so
        # its freshness timestamp never advertises an incomplete capture.
        reports.render_start_here(config, conn)
        if check_repository:
            check, lock_recovery = storage.restic_repository_check(config, ["check"])
            utils.progress("capture: repository metadata check complete")
            check_status = "ok" if check.returncode == 0 else "failed"
            check_detail = " ".join(
                value for value in (lock_recovery, check.stdout, check.stderr) if value
            )
            catalog.record_health(conn, "restic-metadata", check_status, utils.compact(check_detail, 1000))
        result = {
            "capture_id": current_capture,
            "status": status,
            "restic_snapshot": local_snapshot,
            "offsite_snapshot": offsite_snapshot,
            "counts": counters,
            "indexing": index_counts,
            "reasoning": reasoning_counts,
            "warnings": warnings,
        }
        utils.write_json(state / "latest.json", result)
        reports.render_status(config, conn)
        staging.remove_captured_inbox(inbox_items)
        shutil.rmtree(stage)
        return result
    except Exception as exc:
        conn.execute(
            "UPDATE captures SET finished_utc=?,status='failed',error=? WHERE capture_id=?",
            (utils.utc_now(), repr(exc), current_capture),
        )
        conn.commit()
        reports.render_status(config, conn)
        raise
    finally:
        conn.close()


def import_legacy(config: Dict[str, Any], name: str = "legacy-workspaces-pre-dedup") -> Dict[str, Any]:
    storage.ensure_repository(config)
    paths = [str(pathlib.Path(path).resolve()) for path in config.get("legacy_workspaces", []) if pathlib.Path(path).exists()]
    if not paths:
        raise RuntimeError("no legacy workspaces exist")
    conn = catalog.connect_catalog(config)
    try:
        existing = conn.execute("SELECT * FROM legacy_backups WHERE name=? AND status='complete'", (name,)).fetchone()
        if existing:
            return {"name": name, "snapshot_id": existing["snapshot_id"], "already_complete": True}
        result = storage.restic(
            config,
            ["backup", "--json", "--tag", "agentarchive", "--tag", "legacy", "--tag", name] + paths,
        )
        snapshot = storage.parse_restic_snapshot(result.stdout or "")
        if not snapshot:
            raise RuntimeError("legacy backup completed without a snapshot ID")
        conn.execute(
            "INSERT OR REPLACE INTO legacy_backups(name,created_utc,snapshot_id,paths_json,status) VALUES(?,?,?,?,?)",
            (name, utils.utc_now(), snapshot, json.dumps(paths), "complete"),
        )
        conn.commit()
        check, lock_recovery = storage.restic_repository_check(config, ["check"])
        status = "ok" if check.returncode == 0 else "failed"
        check_detail = " ".join(
            value for value in (lock_recovery, check.stdout, check.stderr) if value
        )
        catalog.record_health(conn, "legacy-restic-check", status, utils.compact(check_detail, 1000))
        return {"name": name, "snapshot_id": snapshot, "paths": paths, "check": status}
    finally:
        conn.close()


def verify_archive(config: Dict[str, Any], read_data: bool = False, *, catalog_snapshot_checked: bool = False) -> Dict[str, Any]:
    conn = catalog.connect_catalog(config)
    checks: Dict[str, Any] = {}
    try:
        if catalog_snapshot_checked:
            utils.progress("verify: captured SQLite catalog already passed its integrity check")
            checks["catalog"] = "ok"
        else:
            utils.progress("verify: checking the current SQLite catalog")
            catalog_result = conn.execute("PRAGMA quick_check").fetchone()
            checks["catalog"] = catalog_result[0] if catalog_result else "no result"
        args = ["check"]
        if read_data:
            partition = (dt.datetime.now().timetuple().tm_yday % 30) + 1
            args += ["--read-data-subset", f"{partition}/30"]
        utils.progress("verify: checking the local recovery repository")
        repo, lock_recovery = storage.restic_repository_check(config, args)
        checks["restic"] = "ok" if repo.returncode == 0 else utils.compact(repo.stderr or repo.stdout, 1000)
        if lock_recovery:
            checks["restic_lock_recovery"] = (
                "ok"
                if repo.returncode == 0
                else utils.compact(lock_recovery, 1000)
            )
            catalog.record_health(
                conn,
                "restic-lock-recovery",
                "ok" if repo.returncode == 0 else "failed",
                lock_recovery,
            )
        latest = catalog.latest_success(conn)
        if latest and latest["restic_snapshot"] and latest["manifest_sha256"]:
            utils.progress("verify: restoring the latest snapshot sentinel")
            sentinel_ok, sentinel_detail = storage.verify_snapshot_sentinel(
                config,
                str(latest["restic_snapshot"]),
                str(latest["capture_id"]),
                str(latest["manifest_sha256"]),
            )
            checks["restore_sentinel"] = "ok" if sentinel_ok else sentinel_detail
        else:
            checks["restore_sentinel"] = "no successful snapshot with a manifest"
        claude_settings = pathlib.Path(config["sources"]["claude"]["root"]) / "settings.json"
        with contextlib.suppress(Exception):
            value = utils.read_json(claude_settings, {}).get("cleanupPeriodDays")
            checks["claude_cleanupPeriodDays"] = value
        status = "ok" if all(value == "ok" or isinstance(value, int) and value >= 3650 for value in checks.values()) else "failed"
        catalog.record_health(conn, "full-verify" if read_data else "verify", status, json.dumps(checks, sort_keys=True))
        reports.render_status(config, conn)
        result = {"status": status, "checks": checks,
                  "catalog_basis": "checked capture snapshot" if catalog_snapshot_checked else "live catalog"}
        if lock_recovery:
            result["lock_recovery"] = lock_recovery
        utils.progress(f"verify: complete ({status})")
        return result
    finally:
        conn.close()


def backup_archive(config: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Run the normal manual capture and verification as one operation."""
    # The capture checks its consistent catalog copy before storing it. Verify
    # that backup once, rather than rescanning the live catalog and repo twice.
    captured = capture(config, reason, check_repository=False)
    capture_ok = captured.get("status") in SUCCESSFUL_CAPTURE_STATUSES
    verified = verify_archive(config, catalog_snapshot_checked=capture_ok)
    return {
        "status": "ok" if capture_ok and verified.get("status") == "ok" else "failed",
        "capture": captured,
        "verify": verified,
    }
