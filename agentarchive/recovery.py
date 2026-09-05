"""Verified, resumable recovery and cleanup of abandoned staging trees."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
from typing import Any, Dict, List, Optional

from . import audit as staging_audit, catalog, common as utils, config as configuration, metadata, reports, storage
from .common import VERSION


def write_orphan_stage_metadata(
    config: Dict[str, Any],
    conn: sqlite3.Connection,
    stage: pathlib.Path,
    audit: Dict[str, Any],
) -> str:
    meta = stage / "meta"
    meta.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_rows = [
        json.dumps(row, ensure_ascii=False, sort_keys=True)
        for row in audit["files"]
    ]
    manifest_data = gzip.compress(
        (("\n".join(manifest_rows) + "\n") if manifest_rows else "").encode("utf-8"),
        compresslevel=6,
        mtime=0,
    )
    manifest = meta / "manifest.jsonl.gz"
    utils.atomic_write(manifest, manifest_data, 0o600)
    manifest_digest = hashlib.sha256(manifest_data).hexdigest()
    platform_metadata, platform_digest, platform_entries, platform_warnings = (
        metadata.write_platform_metadata(stage)
    )
    prior = conn.execute(
        "SELECT started_utc,finished_utc,reason,status,error FROM captures WHERE capture_id=?",
        (stage.name,),
    ).fetchone()
    capture_meta = {
        "schema_version": 1,
        "capture_id": stage.name,
        "created_utc": utils.utc_now(),
        "reason": "recovery of an incomplete failed staging tree",
        "agentarchive_version": VERSION,
        "manifest_sha256": manifest_digest,
        "platform_metadata_path": platform_metadata.name,
        "platform_metadata_sha256": platform_digest,
        "platform_metadata_entries": platform_entries,
        "counts": audit["counts"],
        "bytes": audit["bytes"],
        "data_fingerprint": audit["data_fingerprint"],
        "warnings": list(audit.get("warnings") or []) + platform_warnings,
        "original_capture": dict(prior) if prior else None,
    }
    utils.write_json(meta / "capture.json", capture_meta)
    utils.write_json(
        meta / "restore-sentinel.json",
        {
            "capture_id": stage.name,
            "manifest_sha256": manifest_digest,
            "platform_metadata_path": platform_metadata.name,
            "platform_metadata_sha256": platform_digest,
            "data_fingerprint": audit["data_fingerprint"],
            "recovery_kind": "incomplete-staging-tree",
            "created_utc": capture_meta["created_utc"],
        },
    )
    write_saved_orphan_audit(stage, audit)
    return manifest_digest


def write_saved_orphan_audit(stage: pathlib.Path, audit: Dict[str, Any]) -> None:
    encoded = json.dumps(audit, ensure_ascii=False, sort_keys=True).encode("utf-8")
    utils.atomic_write(
        stage / "meta" / "orphan-stage-audit.json.gz",
        gzip.compress(encoded, compresslevel=6, mtime=0),
        0o600,
    )


def validate_existing_stage_metadata(
    stage: pathlib.Path,
    prior: sqlite3.Row,
    audit: Dict[str, Any],
) -> str:
    """Retain a failed capture's valid metadata instead of overwriting it."""
    meta = stage / "meta"
    manifest = meta / "manifest.jsonl.gz"
    sentinel_path = meta / "restore-sentinel.json"
    if not manifest.is_file() or not sentinel_path.is_file():
        raise RuntimeError(f"existing failed-stage metadata is incomplete: {stage}")
    manifest_digest = utils.sha256_file(manifest)
    expected_manifest = str(prior["manifest_sha256"] or "")
    if expected_manifest and manifest_digest != expected_manifest:
        raise RuntimeError(
            f"existing failed-stage manifest changed for {stage}: "
            f"expected {expected_manifest}, got {manifest_digest}"
        )
    sentinel = utils.read_json(sentinel_path)
    if not isinstance(sentinel, dict) or (
        sentinel.get("capture_id") != stage.name
        or sentinel.get("manifest_sha256") != manifest_digest
    ):
        raise RuntimeError(f"existing failed-stage sentinel is invalid: {stage}")
    platform_name = str(sentinel.get("platform_metadata_path") or "")
    platform_digest = str(sentinel.get("platform_metadata_sha256") or "")
    platform = meta / platform_name
    if (
        not platform_name
        or pathlib.Path(platform_name).name != platform_name
        or not platform.is_file()
        or (platform_digest and utils.sha256_file(platform) != platform_digest)
    ):
        raise RuntimeError(f"existing failed-stage platform metadata is invalid: {stage}")
    write_saved_orphan_audit(stage, audit)
    return manifest_digest


def persist_orphan_inventory(
    conn: sqlite3.Connection,
    stage_id: str,
    audit: Dict[str, Any],
) -> None:
    conn.execute("DELETE FROM files WHERE capture_id=?", (stage_id,))
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
        [[row.get(field) for field in fields] for row in audit["files"]],
    )


def ensure_orphan_capture_row(
    conn: sqlite3.Connection,
    stage: pathlib.Path,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM captures WHERE capture_id=?",
        (stage.name,),
    ).fetchone()
    if row:
        if row["status"] == "capturing":
            conn.execute(
                "UPDATE captures SET finished_utc=?,status='failed',error=? WHERE capture_id=?",
                (
                    utils.utc_now(),
                    "Recovered stale 'capturing' row after the archive lock became available.",
                    stage.name,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM captures WHERE capture_id=?",
                (stage.name,),
            ).fetchone()
        return row
    conn.execute(
        """
        INSERT INTO captures(capture_id,started_utc,finished_utc,reason,status,error)
        VALUES(?,?,?,?,?,?)
        """,
        (
            stage.name,
            utils.utc_now(),
            utils.utc_now(),
            "recovery of untracked staging tree",
            "failed",
            "Staging directory existed without a catalog capture row.",
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM captures WHERE capture_id=?",
        (stage.name,),
    ).fetchone()


def read_saved_orphan_audit(stage: pathlib.Path) -> Optional[Dict[str, Any]]:
    path = stage / "meta" / "orphan-stage-audit.json.gz"
    if not path.is_file():
        return None
    try:
        value = json.loads(gzip.decompress(path.read_bytes()))
    except Exception as exc:
        raise RuntimeError(f"invalid saved orphan-stage audit {path}: {exc!r}")
    if (
        not isinstance(value, dict)
        or value.get("stage_id") != stage.name
        or not isinstance(value.get("files"), list)
        or not isinstance(value.get("directories"), list)
        or not value.get("data_fingerprint")
    ):
        raise RuntimeError(f"incomplete saved orphan-stage audit: {path}")
    return value


def recover_incomplete_stages(
    config: Dict[str, Any],
    conn: sqlite3.Connection,
    *,
    read_data: bool = False,
) -> Dict[str, Any]:
    audit_result = staging_audit.audit_staging(config, conn)
    audits: List[Dict[str, Any]] = []
    for current_audit in audit_result["_full_audits"]:
        stage = pathlib.Path(current_audit["stage_path"])
        saved_audit = read_saved_orphan_audit(stage)
        if saved_audit:
            if saved_audit["data_fingerprint"] != current_audit["data_fingerprint"]:
                raise RuntimeError(
                    f"refusing to resume recovery for {stage}: its data changed "
                    "after the recovery snapshot was prepared"
                )
            audits.append(saved_audit)
        else:
            prior = conn.execute(
                "SELECT status FROM captures WHERE capture_id=?",
                (stage.name,),
            ).fetchone()
            if not (stage / "meta" / "manifest.jsonl.gz").is_file() or (
                prior is None
                or prior["status"] in ("failed", "capturing")
            ):
                audits.append(current_audit)
    if not audits:
        return {
            "recovered_stages": [],
            "deleted_stages": [],
            "repository_check": "not-needed",
            "audit": staging_audit.public_staging_audit(audit_result),
        }
    by_path = audit_result["_by_path"]
    by_content = audit_result["_by_content"]
    prepared: List[Dict[str, Any]] = []
    for audit in audits:
        stage = pathlib.Path(audit["stage_path"])
        prior = ensure_orphan_capture_row(conn, stage)
        resuming = (
            prior["status"] == "orphan_recovery_pending_cleanup"
            and read_saved_orphan_audit(stage) is not None
        )
        if resuming:
            manifest = stage / "meta" / "manifest.jsonl.gz"
            if not manifest.is_file():
                raise RuntimeError(f"pending recovery has no manifest: {stage}")
            manifest_digest = utils.sha256_file(manifest)
            expected_manifest = str(prior["manifest_sha256"] or "")
            if expected_manifest and manifest_digest != expected_manifest:
                raise RuntimeError(
                    f"pending recovery manifest changed for {stage}: "
                    f"expected {expected_manifest}, got {manifest_digest}"
                )
        else:
            if (stage / "meta" / "manifest.jsonl.gz").is_file():
                manifest_digest = validate_existing_stage_metadata(stage, prior, audit)
            else:
                manifest_digest = write_orphan_stage_metadata(config, conn, stage, audit)
        local_snapshot = ""
        prior_snapshot = str(prior["restic_snapshot"] or "")
        if prior_snapshot:
            ok, _ = storage.verify_snapshot_sentinel(
                config,
                prior_snapshot,
                stage.name,
                manifest_digest,
            )
            if ok:
                local_snapshot = prior_snapshot
        if not local_snapshot:
            tagged = storage.snapshot_for_capture(config, stage.name)
            if tagged:
                ok, _ = storage.verify_snapshot_sentinel(
                    config,
                    tagged,
                    stage.name,
                    manifest_digest,
                )
                if ok:
                    local_snapshot = tagged
        if not local_snapshot:
            local_snapshot = storage.backup_stage(config, stage, stage.name)
        sentinel_ok, sentinel_detail = storage.verify_snapshot_sentinel(
            config,
            local_snapshot,
            stage.name,
            manifest_digest,
        )
        if not sentinel_ok:
            raise RuntimeError(
                f"refusing to clean {stage}: recovered snapshot {local_snapshot} "
                f"failed its restore-sentinel test: {sentinel_detail}"
            )
        offsite_snapshot = (
            str(prior["offsite_snapshot"])
            if resuming and prior["offsite_snapshot"]
            else None
        )
        offsite_error = None
        offsite = config.get("offsite") or {}
        if offsite.get("enabled") and not offsite_snapshot:
            try:
                storage.ensure_repository(config, offsite=True)
                offsite_snapshot = storage.backup_stage(
                    config,
                    stage,
                    stage.name,
                    offsite=True,
                )
                offsite_ok, detail = storage.verify_snapshot_sentinel(
                    config,
                    offsite_snapshot,
                    stage.name,
                    manifest_digest,
                    offsite=True,
                )
                if not offsite_ok:
                    raise RuntimeError(detail)
            except Exception as exc:
                offsite_error = repr(exc)
                offsite_snapshot = None
        after_backup = staging_audit.audit_incomplete_stage(stage, by_path, by_content)
        if after_backup["data_fingerprint"] != audit["data_fingerprint"]:
            raise RuntimeError(
                f"refusing to clean {stage}: its data changed while it was being archived"
            )
        warnings = list(audit.get("warnings") or [])
        if offsite_error:
            warnings.append(f"offsite recovery backup failed: {offsite_error}")
        if resuming:
            conn.execute(
                """
                UPDATE captures SET finished_utc=?,restic_snapshot=?,
                    offsite_snapshot=?,warnings_json=? WHERE capture_id=?
                """,
                (
                    utils.utc_now(),
                    local_snapshot,
                    offsite_snapshot,
                    json.dumps(warnings),
                    stage.name,
                ),
            )
            conn.commit()
        else:
            persist_orphan_inventory(conn, stage.name, audit)
            original_error = str(prior["error"] or "")
            conn.execute(
                """
                UPDATE captures SET finished_utc=?,status='orphan_recovery_pending_cleanup',
                    restic_snapshot=?,offsite_snapshot=?,file_count=?,logical_bytes=?,
                    new_files=?,changed_files=?,unchanged_files=?,deleted_files=0,
                    manifest_sha256=?,warnings_json=?,error=?
                WHERE capture_id=?
                """,
                (
                    utils.utc_now(),
                    local_snapshot,
                    offsite_snapshot,
                    audit["counts"].get("file_entries", 0),
                    audit["bytes"].get("file_entries", 0),
                    audit["counts"].get("not_in_durable_snapshot", 0),
                    audit["counts"].get("content_elsewhere_archived", 0),
                    audit["counts"].get("same_path_archived", 0),
                    manifest_digest,
                    json.dumps(warnings),
                    f"Recovered incomplete stage after original failure: {original_error}",
                    stage.name,
                ),
            )
            conn.commit()
        stable_manifest = configuration.state_root(config) / "manifests" / f"{stage.name}.jsonl.gz"
        utils.atomic_write(stable_manifest, (stage / "meta" / "manifest.jsonl.gz").read_bytes(), 0o600)
        destination = staging_audit.write_staging_audit(config, audit)
        prepared.append(
            {
                "stage": stage,
                "audit": audit,
                "manifest_sha256": manifest_digest,
                "local_snapshot": local_snapshot,
                "offsite_snapshot": offsite_snapshot,
                "offsite_error": offsite_error,
                "audit_path": str(destination),
            }
        )
    check_args = ["check"]
    if read_data:
        check_args.append("--read-data")
    checked, lock_recovery = storage.restic_repository_check(config, check_args)
    if checked.returncode != 0:
        detail = utils.compact(
            " ".join(
                value
                for value in (lock_recovery, checked.stderr, checked.stdout)
                if value
            ),
            2000,
        )
        catalog.record_health(conn, "staging-recovery-restic-check", "failed", detail)
        raise RuntimeError(
            "recovered snapshots were retained, but staging deletion was refused "
            f"because restic verification failed: {detail}"
        )
    catalog.record_health(
        conn,
        "staging-recovery-restic-check",
        "ok",
        "full repository data read" if read_data else "repository metadata and structure",
    )
    deleted: List[str] = []
    for item in prepared:
        stage = item["stage"]
        current = staging_audit.audit_incomplete_stage(stage, by_path, by_content)
        if current["data_fingerprint"] != item["audit"]["data_fingerprint"]:
            raise RuntimeError(
                f"refusing to clean {stage}: its data changed after repository verification"
            )
        quarantine = configuration.state_root(config) / "staging-trash" / stage.name
        if quarantine.exists():
            raise RuntimeError(f"staging quarantine path already exists: {quarantine}")
        os.replace(stage, quarantine)
        shutil.rmtree(quarantine)
        final_status = (
            "recovered_orphan_offsite_failed"
            if item["offsite_error"]
            else "recovered_orphan"
        )
        conn.execute(
            "UPDATE captures SET finished_utc=?,status=? WHERE capture_id=?",
            (utils.utc_now(), final_status, stage.name),
        )
        conn.commit()
        deleted.append(stage.name)
    reports.render_status(config, conn)
    result = {
        "recovered_stages": [
            {
                "stage_id": item["stage"].name,
                "restic_snapshot": item["local_snapshot"],
                "offsite_snapshot": item["offsite_snapshot"],
                "manifest_sha256": item["manifest_sha256"],
                "data_fingerprint": item["audit"]["data_fingerprint"],
                "audit_path": item["audit_path"],
                "file_entries": item["audit"]["counts"].get("file_entries", 0),
                "logical_bytes": item["audit"]["bytes"].get("file_entries", 0),
                "previously_unarchived_entries": item["audit"]["counts"].get(
                    "not_in_durable_snapshot", 0
                ),
                "previously_unarchived_bytes": item["audit"]["bytes"].get(
                    "not_in_durable_snapshot", 0
                ),
            }
            for item in prepared
        ],
        "deleted_stages": deleted,
        "repository_check": "read-data" if read_data else "metadata",
        "audit": staging_audit.public_staging_audit(audit_result),
    }
    utils.write_json(configuration.state_root(config) / "staging-cleanup-result.json", result)
    return result
