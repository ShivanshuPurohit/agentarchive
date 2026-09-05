"""Portable raw-replica manifests and byte-identity verification."""

from __future__ import annotations

import collections
import gzip
import hashlib
import json
import os
import pathlib
import stat
from typing import Any, Dict, List

from . import common as utils, config as configuration, storage


def raw_replica_definitions(config: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {
            "id": "agentarchive-claude-projects",
            "harness": "claude",
            "path": str(pathlib.Path(config["sources"]["claude"]["root"]) / "projects"),
        },
        {
            "id": "agentarchive-claude-file-history",
            "harness": "claude",
            "path": str(pathlib.Path(config["sources"]["claude"]["root"]) / "file-history"),
        },
        {
            "id": "agentarchive-codex-sessions",
            "harness": "codex",
            "path": str(pathlib.Path(config["sources"]["codex"]["root"]) / "sessions"),
        },
        {
            "id": "agentarchive-codex-archived-sessions",
            "harness": "codex",
            "path": str(pathlib.Path(config["sources"]["codex"]["root"]) / "archived_sessions"),
        },
        {
            "id": "agentarchive-codex-attachments",
            "harness": "codex",
            "path": str(pathlib.Path(config["sources"]["codex"]["root"]) / "attachments"),
        },
    ]


def raw_replica_inventory(config: Dict[str, Any]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    conflicts: List[str] = []
    counts = collections.Counter()
    ignored_names = {".stfolder", ".stignore"}
    for definition in raw_replica_definitions(config):
        root = pathlib.Path(definition["path"]).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"raw-replica source is unavailable: {root}")
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part == ".stversions" for part in relative.parts):
                continue
            if path.name in ignored_names or path.name.startswith(".syncthing."):
                continue
            if path.is_dir() and not path.is_symlink():
                continue
            info = path.lstat()
            coordinate = f"{definition['id']}:{relative.as_posix()}"
            if ".sync-conflict-" in path.name:
                conflicts.append(coordinate)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(str(path))
                kind = "symlink"
                size = len(target.encode("utf-8", errors="surrogateescape"))
                digest = utils.sha256_text("symlink\0" + target)
            elif stat.S_ISREG(info.st_mode):
                target = None
                kind = "file"
                size = info.st_size
                digest = utils.sha256_file(path)
            else:
                raise RuntimeError(f"raw-replica path is not a regular file or symlink: {path}")
            rows.append(
                {
                    "folder_id": definition["id"],
                    "relpath": relative.as_posix(),
                    "file_type": kind,
                    "size_bytes": size,
                    "mtime_ns": info.st_mtime_ns,
                    "sha256": digest,
                    "link_target": target,
                }
            )
            counts["entries"] += 1
            counts["bytes"] += size
            counts[kind] += 1
    canonical = "".join(
        json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        for row in rows
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "generated_utc": utils.utc_now(),
        "device_id": storage.device_identity(config),
        "folders": raw_replica_definitions(config),
        "counts": dict(counts),
        "tree_sha256": hashlib.sha256(canonical).hexdigest(),
        "conflicts": conflicts,
        "files": rows,
    }


def write_raw_replica_manifest(
    config: Dict[str, Any],
    *,
    acknowledge_excluded_history: bool = False,
) -> Dict[str, Any]:
    excluded = [
        str(value)
        for value in config.get("excluded_workspaces", [])
        if isinstance(value, str) and value.strip()
    ]
    if excluded and not acknowledge_excluded_history:
        raise RuntimeError(
            "raw-replica preparation is blocked because these folders include excluded "
            "workspace rollouts: " + ", ".join(excluded)
            + ". Re-run only after explicit permission, using "
            "--acknowledge-excluded-history."
        )
    inventory = raw_replica_inventory(config)
    if inventory["conflicts"]:
        raise RuntimeError(
            f"raw-replica preflight found {len(inventory['conflicts'])} unresolved "
            f"Syncthing conflict files; first: {inventory['conflicts'][0]}"
        )
    timestamp = utils.capture_id()
    destination = configuration.state_root(config) / "manifests" / f"raw-replica-{inventory['device_id']}-{timestamp}.json.gz"
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True).encode("utf-8")
    utils.atomic_write(destination, gzip.compress(encoded, compresslevel=6, mtime=0), 0o600)
    return {
        "status": "ready",
        "manifest": str(destination),
        "device_id": inventory["device_id"],
        "tree_sha256": inventory["tree_sha256"],
        "counts": inventory["counts"],
        "folders": inventory["folders"],
        "writer_contract": (
            "Never resume or append the same session UUID on two devices at once. "
            "Any .sync-conflict-* file is retained but blocks import until reviewed."
        ),
    }


def verify_raw_replica_manifest(
    config: Dict[str, Any],
    manifest_path: pathlib.Path,
    *,
    acknowledge_excluded_history: bool = False,
) -> Dict[str, Any]:
    excluded = [
        str(value)
        for value in config.get("excluded_workspaces", [])
        if isinstance(value, str) and value.strip()
    ]
    if excluded and not acknowledge_excluded_history:
        raise RuntimeError(
            "raw-replica verification requires explicit permission for excluded workspace "
            "rollouts: " + ", ".join(excluded)
        )
    try:
        expected = json.loads(gzip.decompress(manifest_path.read_bytes()))
    except Exception as exc:
        raise RuntimeError(f"invalid raw-replica manifest {manifest_path}: {exc!r}") from exc
    if not isinstance(expected, dict) or not isinstance(expected.get("files"), list):
        raise RuntimeError(f"incomplete raw-replica manifest: {manifest_path}")
    current = raw_replica_inventory(config)
    expected_rows = {
        (str(row.get("folder_id")), str(row.get("relpath"))): row
        for row in expected["files"]
        if isinstance(row, dict)
    }
    current_rows = {
        (str(row.get("folder_id")), str(row.get("relpath"))): row
        for row in current["files"]
    }
    missing = sorted(set(expected_rows) - set(current_rows))
    extra = sorted(set(current_rows) - set(expected_rows))
    changed = sorted(
        key for key in set(expected_rows) & set(current_rows)
        if (
            expected_rows[key].get("file_type"),
            expected_rows[key].get("size_bytes"),
            expected_rows[key].get("sha256"),
            expected_rows[key].get("link_target"),
        ) != (
            current_rows[key].get("file_type"),
            current_rows[key].get("size_bytes"),
            current_rows[key].get("sha256"),
            current_rows[key].get("link_target"),
        )
    )
    status = "identical" if not missing and not extra and not changed and not current["conflicts"] else "different"
    return {
        "status": status,
        "manifest": str(manifest_path),
        "expected_tree_sha256": expected.get("tree_sha256"),
        "current_tree_sha256": current["tree_sha256"],
        "missing": [f"{folder}:{path}" for folder, path in missing[:50]],
        "extra": [f"{folder}:{path}" for folder, path in extra[:50]],
        "changed": [f"{folder}:{path}" for folder, path in changed[:50]],
        "missing_count": len(missing),
        "extra_count": len(extra),
        "changed_count": len(changed),
        "conflicts": current["conflicts"][:50],
    }
