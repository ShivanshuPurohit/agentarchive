"""Transactional configuration and migration of a restic replica."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional

from . import catalog, common as utils, config as configuration, storage


def configure_offsite(
    config_path: pathlib.Path,
    repository: str,
    password_command: Optional[str],
    *,
    migrate_existing: bool = True,
    acknowledge_excluded_history: bool = False,
) -> Dict[str, Any]:
    """Transactionally initialize, populate, and verify an off-device repository.

    Configuration is committed only after the destination is usable.  A failed
    or interrupted initial copy is therefore safe to retry and cannot make the
    daily capture path advertise a repository that is not ready.
    """
    repository = repository.strip()
    if not repository:
        raise RuntimeError("offsite repository must not be empty")
    config = configuration.load_config(config_path)
    excluded = [
        str(value)
        for value in config.get("excluded_workspaces", [])
        if isinstance(value, str) and value.strip()
    ]
    if excluded and not acknowledge_excluded_history:
        raise RuntimeError(
            "offsite setup is blocked because historical snapshots can contain "
            "global database metadata for excluded workspaces: "
            + ", ".join(excluded)
            + ". Re-run only after explicit permission, using "
            "--acknowledge-excluded-history."
        )

    prior_offsite = config.get("offsite") or {}
    environment = dict(prior_offsite.get("environment") or {})
    if repository.startswith("rclone:") and "PATH" not in environment:
        binary_dir = str(pathlib.Path(str(config["restic_binary"])).parent)
        environment["PATH"] = f"{binary_dir}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    candidate = dict(config)
    candidate["offsite"] = {
        "enabled": True,
        "repository": repository,
        "password_command": password_command or config["password_command"],
        "environment": environment,
    }

    probe = storage.restic(candidate, ["snapshots", "--json"], offsite=True, check=False)
    created = False
    if probe.returncode != 0:
        if probe.returncode != 10:
            raise RuntimeError(
                "cannot access the proposed offsite repository: "
                + utils.compact(probe.stderr or probe.stdout or "unknown restic error", 4000)
            )
        initialized = storage.restic(
            candidate,
            [
                "init",
                "--from-repo",
                str(config["local_repository"]),
                "--from-password-command",
                str(config["password_command"]),
                "--copy-chunker-params",
            ],
            offsite=True,
            check=False,
        )
        if initialized.returncode != 0:
            raise RuntimeError(
                "offsite repository initialization failed: "
                + utils.compact(initialized.stderr or initialized.stdout or "unknown restic error", 4000)
            )
        created = True

    local_repository_config = storage.restic_repository_config(config)
    offsite_repository_config = storage.restic_repository_config(candidate, offsite=True)
    local_chunker = str(local_repository_config.get("chunker_polynomial") or "")
    offsite_chunker = str(offsite_repository_config.get("chunker_polynomial") or "")
    if not local_chunker or local_chunker != offsite_chunker:
        raise RuntimeError(
            "offsite repository uses incompatible chunker parameters; choose a fresh, "
            "empty destination so incremental copies can deduplicate correctly"
        )

    local_rows = storage.restic_snapshot_inventory(config)
    source_ids = {
        str(row["id"])
        for row in local_rows
        if row.get("id")
    }
    copy_output = ""
    if migrate_existing and source_ids:
        copied = storage.restic(
            candidate,
            [
                "copy",
                "--host",
                "",
                "--from-repo",
                str(config["local_repository"]),
                "--from-password-command",
                str(config["password_command"]),
            ],
            offsite=True,
            check=False,
        )
        if copied.returncode != 0:
            raise RuntimeError(
                "historical offsite migration failed; the partial copy is resumable: "
                + utils.compact(copied.stderr or copied.stdout or "unknown restic error", 4000)
            )
        copy_output = utils.compact(copied.stdout or copied.stderr or "", 2000)

    offsite_rows = storage.restic_snapshot_inventory(candidate, offsite=True)
    represented_source_ids = {
        str(row.get("original") or row.get("id"))
        for row in offsite_rows
        if row.get("original") or row.get("id")
    }
    missing = sorted(source_ids - represented_source_ids) if migrate_existing else []
    if missing:
        raise RuntimeError(
            f"offsite migration is incomplete: {len(missing)} local snapshots are absent; "
            f"first missing snapshot is {missing[0]}"
        )

    checked, lock_recovery = storage.restic_repository_check(
        candidate,
        ["check"],
        offsite=True,
    )
    if checked.returncode != 0:
        detail = " ".join(
            value for value in (lock_recovery, checked.stderr, checked.stdout) if value
        )
        raise RuntimeError(f"offsite repository verification failed: {utils.compact(detail, 4000)}")

    sentinel_verified = False
    if migrate_existing and catalog.catalog_path(config).is_file():
        conn = catalog.connect_catalog_readonly(config)
        try:
            latest = catalog.latest_success(conn)
        finally:
            conn.close()
        if latest and latest["restic_snapshot"] and latest["manifest_sha256"]:
            source_snapshot = str(latest["restic_snapshot"])
            destination = next(
                (
                    str(row["id"])
                    for row in reversed(offsite_rows)
                    if row.get("id")
                    and str(row.get("original") or row.get("id")) == source_snapshot
                ),
                "",
            )
            if not destination:
                raise RuntimeError(
                    f"latest local snapshot {source_snapshot} has no offsite copy"
                )
            sentinel_verified, detail = storage.verify_snapshot_sentinel(
                candidate,
                destination,
                str(latest["capture_id"]),
                str(latest["manifest_sha256"]),
                offsite=True,
            )
            if not sentinel_verified:
                raise RuntimeError(
                    f"offsite restore-sentinel verification failed: {detail}"
                )

    utils.write_json(config_path, candidate)
    return {
        **candidate["offsite"],
        "repository_created": created,
        "migrated_existing": migrate_existing,
        "local_snapshot_count": len(source_ids),
        "offsite_snapshot_count": len(offsite_rows),
        "restore_sentinel_verified": sentinel_verified,
        "repository_check": "ok",
        "copy_output": copy_output,
    }
