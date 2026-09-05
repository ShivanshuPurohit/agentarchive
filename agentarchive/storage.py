"""Local storage, locking, restic operations, and snapshot verification."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import common as utils, config as configuration
from .common import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE


@contextlib.contextmanager
def exclusive_lock(config: Dict[str, Any], wait: bool = True) -> Iterator[None]:
    path = configuration.state_root(config) / "capture.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as handle:
        operation = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError:
            raise RuntimeError("another archive operation is already running")
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def keychain_has_password(config: Optional[Dict[str, Any]] = None) -> bool:
    config = config or {}
    result = utils.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            config.get("keychain_account", KEYCHAIN_ACCOUNT),
            "-s",
            config.get("keychain_service", KEYCHAIN_SERVICE),
        ],
        check=False,
    )
    return result.returncode == 0


def ensure_keychain_password(config: Optional[Dict[str, Any]] = None) -> None:
    config = config or {}
    if keychain_has_password(config):
        return
    password = secrets.token_urlsafe(64)
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-a",
            config.get("keychain_account", KEYCHAIN_ACCOUNT),
            "-s",
            config.get("keychain_service", KEYCHAIN_SERVICE),
            "-w",
            password,
            "-U",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"unable to store restic password in login Keychain: {result.stderr}")


def restic_env(config: Dict[str, Any], offsite: bool = False) -> Dict[str, str]:
    env = os.environ.copy()
    if offsite:
        target = config.get("offsite") or {}
        env["RESTIC_REPOSITORY"] = str(target.get("repository") or "")
        command = str(target.get("password_command") or config["password_command"])
        for key, value in (target.get("environment") or {}).items():
            env[str(key)] = str(value)
    else:
        env["RESTIC_REPOSITORY"] = str(config["local_repository"])
        command = str(config["password_command"])
    env["RESTIC_PASSWORD_COMMAND"] = command
    env["RESTIC_CACHE_DIR"] = str(configuration.state_root(config) / "restic-cache")
    env["RESTIC_HOST"] = str(config.get("restic_host") or "agentarchive")
    return env


def restic(config: Dict[str, Any], args: Sequence[str], *, cwd: Optional[pathlib.Path] = None, offsite: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    binary = str(config["restic_binary"])
    return utils.run([binary] + list(args), cwd=cwd, env=restic_env(config, offsite), check=check, capture=True)


def restic_repository_check(
    config: Dict[str, Any],
    args: Sequence[str],
    *,
    offsite: bool = False,
) -> Tuple[subprocess.CompletedProcess, Optional[str]]:
    """Run a repository check, removing only Restic-classified stale locks once."""
    if not args or args[0] != "check":
        raise ValueError("restic_repository_check requires a check command")
    def invoke(current_args: Sequence[str]) -> subprocess.CompletedProcess:
        if offsite:
            return restic(config, current_args, offsite=True, check=False)
        return restic(config, current_args, check=False)

    checked = invoke(args)
    if checked.returncode != 11:
        return checked, None
    unlocked = invoke(["unlock"])
    unlock_detail = utils.compact(
        (unlocked.stdout or "") + " " + (unlocked.stderr or ""),
        1000,
    ) or "restic unlock produced no output"
    if unlocked.returncode != 0:
        return checked, f"stale-lock recovery failed: {unlock_detail}"
    retried = invoke(args)
    return retried, f"stale-lock recovery attempted: {unlock_detail}"


def ensure_repository(config: Dict[str, Any], offsite: bool = False) -> None:
    if offsite and not (config.get("offsite") or {}).get("enabled"):
        return
    probe = restic(config, ["snapshots", "--json"], offsite=offsite, check=False)
    if probe.returncode == 0:
        return
    init = restic(config, ["init"], offsite=offsite, check=False)
    if init.returncode != 0:
        raise RuntimeError(utils.compact(init.stderr or init.stdout or "restic init failed", 4000))


def restic_snapshot_inventory(
    config: Dict[str, Any], *, offsite: bool = False
) -> List[Dict[str, Any]]:
    result = restic(
        config,
        ["snapshots", "--json"],
        offsite=offsite,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            utils.compact(result.stderr or result.stdout or "restic snapshot inventory failed", 4000)
        )
    try:
        rows = json.loads(result.stdout or "[]")
    except Exception as exc:
        raise RuntimeError(f"restic returned invalid snapshot JSON: {exc!r}") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("restic returned a non-list snapshot inventory")
    return rows


def restic_repository_config(
    config: Dict[str, Any], *, offsite: bool = False
) -> Dict[str, Any]:
    result = restic(config, ["cat", "config"], offsite=offsite, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            utils.compact(result.stderr or result.stdout or "restic repository config failed", 4000)
        )
    try:
        value = json.loads(result.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"restic returned invalid repository config JSON: {exc!r}") from exc
    if not isinstance(value, dict) or not value.get("id"):
        raise RuntimeError("restic returned an incomplete repository config")
    return value


def copy_clone(source: pathlib.Path, destination: pathlib.Path, warnings: List[str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink():
        destination.symlink_to(os.readlink(str(source)))
        return
    command = ["/bin/cp", "-c", "-p"]
    if source.is_dir():
        command.append("-R")
    command.extend([str(source), str(destination)])
    result = utils.run(command, check=False)
    if result.returncode == 0:
        return
    warnings.append(f"APFS clone reported an issue for {source}: {utils.compact(result.stderr, 500)}")
    if destination.exists() or destination.is_symlink():
        return
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def device_identity(config: Dict[str, Any]) -> str:
    """Return a stable opaque ID for this installation, never just a hostname."""
    path = configuration.state_root(config) / "device-id"
    if path.is_file():
        value = path.read_text(encoding="ascii", errors="strict").strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{8,96}", value):
            return value
        raise RuntimeError(f"invalid AgentArchive device identity: {path}")
    try:
        hostname = os.uname().nodename
    except (AttributeError, OSError):
        hostname = "device"
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", hostname).strip("-._") or "device"
    value = f"{label[:40]}-{uuid.uuid4().hex[:16]}"
    utils.atomic_write(path, (value + "\n").encode("ascii"), 0o600)
    return value


def clear_sqlite_temp_sidecars(database: pathlib.Path, require_empty_journal: bool = False) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = pathlib.Path(str(database) + suffix)
        if not sidecar.exists():
            continue
        if require_empty_journal and suffix in ("-wal", "-journal") and sidecar.stat().st_size:
            raise RuntimeError(f"non-empty temporary SQLite journal remains: {sidecar}")
        sidecar.unlink()


def sqlite_backup(source: pathlib.Path, destination: pathlib.Path) -> Tuple[bool, str]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    for attempt in range(3):
        try:
            src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
            dst = sqlite3.connect(str(temp), timeout=60)
            try:
                src.backup(dst, pages=8192, sleep=0.05)
                result = dst.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"quick_check returned {result}")
            finally:
                dst.close()
                src.close()
            clear_sqlite_temp_sidecars(temp, require_empty_journal=True)
            os.chmod(temp, 0o600)
            os.replace(temp, destination)
            source_stat = source.stat()
            os.utime(destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            return True, "ok"
        except Exception as exc:
            if temp.exists():
                temp.unlink()
            clear_sqlite_temp_sidecars(temp)
            if attempt == 2:
                # The bundled app may write a newer SQLite/WAL variant than the
                # system Python module understands.  The system sqlite3 CLI is
                # newer on this machine, so retain a consistent fallback.
                quoted = str(temp).replace("'", "''")
                fallback = utils.run(
                    ["/usr/bin/sqlite3", str(source), f".timeout 60000", f".backup '{quoted}'"],
                    check=False,
                )
                if fallback.returncode == 0 and temp.is_file():
                    check = utils.run(
                        ["/usr/bin/sqlite3", str(temp), "PRAGMA quick_check;"],
                        check=False,
                    )
                    if check.returncode == 0 and (check.stdout or "").strip() == "ok":
                        clear_sqlite_temp_sidecars(temp, require_empty_journal=True)
                        os.chmod(temp, 0o600)
                        os.replace(temp, destination)
                        source_stat = source.stat()
                        os.utime(destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
                        return True, "ok-cli-fallback"
                return False, f"{exc!r}; cli fallback: {utils.compact(fallback.stderr or fallback.stdout, 500)}"
            time.sleep(1 + attempt)
    return False, "unknown backup failure"


def parse_restic_snapshot(output: str) -> str:
    snapshot_id = ""
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict) and value.get("snapshot_id"):
            snapshot_id = str(value["snapshot_id"])
    if not snapshot_id:
        try:
            value = json.loads(output)
            if isinstance(value, dict):
                snapshot_id = str(value.get("snapshot_id") or "")
        except Exception:
            pass
    return snapshot_id


def latest_restic_snapshot_id(config: Dict[str, Any], offsite: bool = False) -> str:
    location_tag = "offsite" if offsite else "local"
    result = restic(
        config,
        ["snapshots", "--json", "--tag", "agentarchive", "--tag", location_tag],
        offsite=offsite,
        check=False,
    )
    if result.returncode != 0:
        return ""
    try:
        snapshots = json.loads(result.stdout or "[]")
    except Exception:
        return ""
    if not isinstance(snapshots, list):
        return ""
    matching = [
        row for row in snapshots
        if isinstance(row, dict)
        and "agentarchive" in (row.get("tags") or [])
        and location_tag in (row.get("tags") or [])
        and any(
            str(tag).startswith("capture-")
            for tag in (row.get("tags") or [])
        )
        and row.get("id")
    ]
    if not matching:
        return ""
    def capture_order(row: Dict[str, Any]) -> Tuple[str, str]:
        capture_tags = sorted(
            str(tag).removeprefix("capture-")
            for tag in (row.get("tags") or [])
            if str(tag).startswith("capture-")
        )
        return (
            capture_tags[-1] if capture_tags else "",
            str(row.get("time") or ""),
        )

    matching.sort(key=capture_order)
    return str(matching[-1]["id"])


def backup_stage(config: Dict[str, Any], stage: pathlib.Path, current_capture: str, offsite: bool = False) -> str:
    args = [
        "backup",
        "--json",
        "--tag",
        "agentarchive",
        "--tag",
        f"capture-{current_capture}",
        "--tag",
        "offsite" if offsite else "local",
    ]
    parent = latest_restic_snapshot_id(config, offsite=offsite)
    if parent:
        args.extend(["--parent", parent])
    args.extend(["data", "meta"])
    result = restic(config, args, cwd=stage, offsite=offsite, check=False)
    if result.returncode != 0 and parent:
        # Parent metadata is only an optimization. A missing/pruned/incompatible
        # parent must never prevent a new content-addressed backup.
        retry_args = [value for value in args if value not in ("--parent", parent)]
        result = restic(config, retry_args, cwd=stage, offsite=offsite, check=False)
    if result.returncode != 0:
        raise RuntimeError(utils.compact(result.stderr or result.stdout or "restic backup failed", 4000))
    snapshot = parse_restic_snapshot(result.stdout or "")
    if not snapshot:
        raise RuntimeError("restic completed without returning a snapshot ID")
    return snapshot


def known_restic_snapshot_ids(config: Dict[str, Any], offsite: bool = False) -> set[str]:
    result = restic(config, ["snapshots", "--json"], offsite=offsite, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"cannot prove staging coverage because restic snapshots are unavailable: "
            f"{utils.compact(result.stderr or result.stdout, 1000)}"
        )
    try:
        snapshots = json.loads(result.stdout or "[]")
    except Exception as exc:
        raise RuntimeError(f"restic returned invalid snapshot JSON: {exc!r}")
    if not isinstance(snapshots, list):
        raise RuntimeError("restic returned a non-list snapshot inventory")
    return {
        str(row["id"])
        for row in snapshots
        if isinstance(row, dict) and row.get("id")
    }


def snapshot_for_capture(
    config: Dict[str, Any],
    current_capture: str,
    *,
    offsite: bool = False,
) -> str:
    location = "offsite" if offsite else "local"
    result = restic(
        config,
        [
            "snapshots",
            "--json",
            "--tag",
            "agentarchive",
            "--tag",
            f"capture-{current_capture}",
            "--tag",
            location,
        ],
        offsite=offsite,
        check=False,
    )
    if result.returncode != 0:
        return ""
    try:
        snapshots = json.loads(result.stdout or "[]")
    except Exception:
        return ""
    matching = [
        row
        for row in snapshots
        if isinstance(row, dict)
        and row.get("id")
        and f"capture-{current_capture}" in (row.get("tags") or [])
        and location in (row.get("tags") or [])
    ]
    matching.sort(key=lambda row: str(row.get("time") or ""))
    return str(matching[-1]["id"]) if matching else ""


def verify_snapshot_sentinel(
    config: Dict[str, Any],
    snapshot: str,
    capture: str,
    manifest_digest: str,
    *,
    offsite: bool = False,
) -> Tuple[bool, str]:
    last_detail = ""
    for path in ("/meta/restore-sentinel.json", "meta/restore-sentinel.json"):
        dumped = restic(
            config,
            ["dump", snapshot, path],
            offsite=offsite,
            check=False,
        )
        if dumped.returncode != 0:
            last_detail = utils.compact(dumped.stderr or dumped.stdout, 1000)
            continue
        try:
            sentinel = json.loads(dumped.stdout or "{}")
        except Exception as exc:
            last_detail = f"invalid sentinel JSON: {exc!r}"
            continue
        if (
            sentinel.get("capture_id") == capture
            and sentinel.get("manifest_sha256") == manifest_digest
        ):
            platform_digest = str(
                sentinel.get("platform_metadata_sha256") or ""
            )
            if not platform_digest:
                return True, "ok"
            platform_name = pathlib.PurePosixPath(
                str(sentinel.get("platform_metadata_path") or "")
            ).name
            if not platform_name:
                return False, "sentinel names no platform-metadata sidecar"
            platform_detail = ""
            for metadata_path in (
                f"/meta/{platform_name}",
                f"meta/{platform_name}",
            ):
                metadata = restic(
                    config,
                    ["dump", snapshot, metadata_path],
                    offsite=offsite,
                    check=False,
                )
                if metadata.returncode != 0:
                    platform_detail = utils.compact(
                        metadata.stderr or metadata.stdout,
                        1000,
                    )
                    continue
                restored_digest = hashlib.sha256(
                    (metadata.stdout or "").encode("utf-8")
                ).hexdigest()
                if restored_digest == platform_digest:
                    return True, "ok"
                platform_detail = (
                    f"platform-metadata digest mismatch: expected "
                    f"{platform_digest}, got {restored_digest}"
                )
            return False, platform_detail or "platform-metadata sidecar is unavailable"
        last_detail = (
            f"sentinel mismatch: capture={sentinel.get('capture_id')!r}, "
            f"manifest={sentinel.get('manifest_sha256')!r}"
        )
    return False, last_detail or "restore sentinel is unavailable"
