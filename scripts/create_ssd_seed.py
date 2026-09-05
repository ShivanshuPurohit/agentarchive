#!/usr/bin/env python3
"""Create a verified, privacy-filtered AgentArchive seed on an exFAT SSD."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable


REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from privacy_filter import transcript_is_excluded  # noqa: E402
from agentarchive.runtime import copy_runtime


UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def progress(message: str) -> None:
    print(f"ssd-seed: {message}", flush=True)


def run(argv: Iterable[str]) -> None:
    subprocess.run(list(argv), check=True)


def clone(source: pathlib.Path, destination: pathlib.Path) -> None:
    if not source.exists() and not source.is_symlink():
        destination.mkdir(parents=True, exist_ok=True)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(("/bin/cp", "-c", "-p", "-R", str(source), str(destination)))


def copy_file(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def sqlite_quick_check(path: pathlib.Path) -> None:
    wal = pathlib.Path(f"{path}-wal")
    immutable = not wal.exists() or wal.stat().st_size == 0
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite quick_check failed for {path}: {result!r}")


def portable_sqlite_copy(source: pathlib.Path, destination: pathlib.Path) -> None:
    """Create a self-contained DELETE-journal database without source writes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    wal = pathlib.Path(f"{source}-wal")
    immutable = not wal.exists() or wal.stat().st_size == 0
    uri = source.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    source_connection = sqlite3.connect(uri, uri=True, timeout=60)
    destination_connection = sqlite3.connect(str(temporary), timeout=60)
    try:
        source_connection.backup(destination_connection, pages=8192, sleep=0.05)
        mode = destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        result = destination_connection.execute("PRAGMA quick_check").fetchone()
        if not mode or str(mode[0]).lower() != "delete" or not result or result[0] != "ok":
            raise RuntimeError(
                f"portable SQLite conversion failed for {source}: mode={mode!r}, check={result!r}"
            )
    finally:
        destination_connection.close()
        source_connection.close()
    os.replace(temporary, destination)


def remove_path(path: pathlib.Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def current_raw_snapshot(config: Dict[str, Any], snapshot: pathlib.Path) -> Dict[str, int]:
    roots = {
        "claude/projects": pathlib.Path(config["sources"]["claude"]["root"]) / "projects",
        "claude/file-history": pathlib.Path(config["sources"]["claude"]["root"]) / "file-history",
        "codex/sessions": pathlib.Path(config["sources"]["codex"]["root"]) / "sessions",
        "codex/archived_sessions": pathlib.Path(config["sources"]["codex"]["root"])
        / "archived_sessions",
        "codex/attachments": pathlib.Path(config["sources"]["codex"]["root"]) / "attachments",
    }
    raw = snapshot / "raw"
    for relative, source in roots.items():
        clone(source.expanduser().resolve(), raw / relative)

    excluded_ids: set[str] = set()
    excluded_claude_projects: set[pathlib.Path] = set()
    excluded_claude_transcripts = 0
    excluded_codex_files: list[pathlib.Path] = []
    claude_projects = raw / "claude" / "projects"
    for transcript in claude_projects.rglob("*.jsonl"):
        if not transcript_is_excluded(config, transcript):
            continue
        excluded_claude_transcripts += 1
        relative = transcript.relative_to(claude_projects)
        excluded_claude_projects.add(claude_projects / relative.parts[0])
        excluded_ids.update(UUID_RE.findall(transcript.name))
    for root in (raw / "codex" / "sessions", raw / "codex" / "archived_sessions"):
        for transcript in root.rglob("*.jsonl"):
            if transcript_is_excluded(config, transcript):
                excluded_codex_files.append(transcript)
                excluded_ids.update(UUID_RE.findall(transcript.name))

    for project in sorted(excluded_claude_projects):
        remove_path(project)
    for transcript in excluded_codex_files:
        remove_path(transcript)
    if excluded_ids:
        candidates = sorted(raw.rglob("*"), key=lambda path: len(path.parts), reverse=True)
        for candidate in candidates:
            if candidate.exists() and any(value in candidate.name for value in excluded_ids):
                remove_path(candidate)

    remaining_private = sum(
        transcript_is_excluded(config, path)
        for path in raw.rglob("*.jsonl")
        if path.is_file()
    )
    if remaining_private:
        raise RuntimeError(
            f"privacy filter left {remaining_private} excluded transcripts in the raw snapshot"
        )
    return {
        "excluded_transcripts": excluded_claude_transcripts + len(excluded_codex_files),
        "excluded_claude_project_trees": len(excluded_claude_projects),
        "excluded_session_ids": len(excluded_ids),
    }


def legacy_layout(config: Dict[str, Any]):
    """Resolve configured legacy archives without assuming a machine or date."""
    for entry in config.get("legacy_indexes", []):
        database = pathlib.Path(entry["database"]).expanduser().resolve()
        workspace = pathlib.Path(entry.get("workspace") or database.parents[2]).expanduser().resolve()
        yield entry, workspace, pathlib.Path("legacy") / entry["harness"]


def legacy_snapshot(config: Dict[str, Any], snapshot: pathlib.Path) -> None:
    (snapshot / "legacy").mkdir(parents=True, exist_ok=True)
    for entry, workspace, target in legacy_layout(config):
        if not entry.get("seed_paths"):
            raise RuntimeError("legacy SSD seeding requires explicit seed_paths for each configured archive")
        selected = [pathlib.Path(value) for value in entry["seed_paths"]]
        if entry.get("start_here"):
            selected.append(pathlib.Path(entry["start_here"]).expanduser().resolve().relative_to(workspace))
        for relative in selected:
            source = (workspace / relative).resolve()
            relative = source.relative_to(workspace)
            clone(source, snapshot / target / relative)
        database = pathlib.Path(entry["database"]).expanduser().resolve()
        portable_sqlite_copy(database, snapshot / target / database.relative_to(workspace))
        for transcript in (snapshot / target).rglob("*.jsonl"):
            if transcript_is_excluded(config, transcript):
                raise RuntimeError("legacy seed blocked: an excluded-workspace transcript is present")
        sqlite_quick_check(snapshot / target / database.relative_to(workspace))


def portable_snapshot(source: pathlib.Path, snapshot: pathlib.Path, config: Dict[str, Any]) -> None:
    target = snapshot / "portable"
    clone(source, target)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = target / f"catalog.sqlite{suffix}"
        if sidecar.exists():
            sidecar.unlink()
    copy_runtime(REPO, target / "bin")
    sqlite_quick_check(target / "catalog.sqlite")
    import_root = pathlib.Path("~/.local/share/AgentArchiveImports/current")
    legacy = []
    for entry, workspace, relative_root in legacy_layout(config):
        portable_entry = {"harness": entry["harness"], "name": entry.get("name", entry["harness"])}
        for key in ("database", "start_here"):
            if entry.get(key):
                relative = pathlib.Path(entry[key]).expanduser().resolve().relative_to(workspace)
                portable_entry[key] = str(import_root / relative_root / relative)
        legacy.append(portable_entry)
    excluded = list(config.get("excluded_workspaces", []))
    for value in list(excluded):
        try:
            relative = pathlib.Path(value).expanduser().relative_to(pathlib.Path.home())
        except ValueError:
            continue
        excluded.append(str(pathlib.Path("~") / relative))
    wsl_config = {
        "schema_version": 1,
        "state_root": str(import_root / "portable"),
        "restic_binary": "restic",
        "local_repository": "~/.local/share/AgentArchiveImports/repository-not-used",
        "password_command": "false",
        "excluded_workspaces": list(dict.fromkeys(excluded)),
        "sources": {
            harness: {"root": f"~/.{harness}",
                      "legacy_cutoff_utc": config.get("sources", {}).get(harness, {}).get("legacy_cutoff_utc", "")}
            for harness in ("claude", "codex")
        },
        "legacy_indexes": legacy,
        "offsite": {"enabled": False, "repository": "", "password_command": "", "environment": {}},
    }
    (target / "config.wsl-readonly.json").write_text(
        json.dumps(wsl_config, indent=2) + "\n", encoding="utf-8"
    )
    (target / "WSL_START_HERE.md").write_text(
        "# AgentArchive on WSL\n\n"
        "Use `agent-archive search 'QUERY'` to search both Claude and Codex. "
        "Incremental raw hits have stable `i:claude:...` or `i:codex:...` IDs; "
        "use `agent-archive context HIT_ID --max-chars 6000` for exact evidence.\n\n"
        "Raw history is merged into `~/.claude/projects`, `~/.claude/file-history`, "
        "`~/.codex/sessions`, `~/.codex/archived_sessions`, and "
        "`~/.codex/attachments`. WSL-specific auth, settings, caches, and mutable "
        "state databases are not imported. Configured historical evidence "
        "is routed through the relocated read-only legacy indexes.\n",
        encoding="utf-8",
    )


def tree_stats(root: pathlib.Path) -> Dict[str, int]:
    files = symlinks = bytes_total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            symlinks += 1
            bytes_total += len(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif path.is_file():
            files += 1
            bytes_total += path.stat().st_size
    return {"files": files, "symlinks": symlinks, "logical_bytes": bytes_total}


def create_tar(source_root: pathlib.Path, member: str, destination: pathlib.Path) -> None:
    temporary = destination.with_name(f".{destination.name}.partial")
    # exFAT is extremely slow with tar's default 10 KiB output records.  Four
    # MiB records keep this a sequential SSD copy without adding compression CPU.
    run(
        (
            "/usr/bin/tar", "-b", "8192", "-cf", str(temporary),
            "-C", str(source_root), member,
        )
    )
    subprocess.run(
        ("/usr/bin/tar", "-tf", str(temporary)),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    os.replace(temporary, destination)


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=pathlib.Path,
        help="seed destination; defaults to ssd_seed_destination in the local config",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=pathlib.Path.home() / "Library/Application Support/AgentArchive/config.json",
    )
    parser.add_argument(
        "--portable-state",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".local/share/AgentArchivePortable",
    )
    args = parser.parse_args()
    config = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
    destination_value = args.destination or config.get("ssd_seed_destination")
    if not destination_value:
        parser.error("provide --destination or set ssd_seed_destination in the local config")
    destination = pathlib.Path(destination_value).expanduser().resolve()
    portable = args.portable_state.expanduser().resolve()
    if not portable.is_dir():
        parser.error(f"portable AgentArchive state is unavailable: {portable}")
    destination.mkdir(parents=True, exist_ok=True)
    if not os.access(destination, os.W_OK):
        parser.error(f"destination is not writable: {destination}")

    seed_id = dt.datetime.now(dt.timezone.utc).strftime("mac-seed-%Y%m%dT%H%M%SZ")
    partial = destination / f".{seed_id}.partial"
    final = destination / seed_id
    if partial.exists() or final.exists():
        parser.error(f"seed path already exists: {partial if partial.exists() else final}")
    partial.mkdir(mode=0o700)
    payload = partial / "payload"
    payload.mkdir()

    with tempfile.TemporaryDirectory(prefix=".agentarchive-ssd-seed-") as temp_text:
        snapshot = pathlib.Path(temp_text)
        progress("cloning a point-in-time copy of the current raw rollout trees")
        privacy = current_raw_snapshot(config, snapshot)
        progress("cloning the portable query catalog and fixed reader")
        portable_snapshot(portable, snapshot, config)
        progress("cloning the historical indexes and raw evidence")
        legacy_snapshot(config, snapshot)
        members = {
            "current-raw-mac.tar": "raw",
            "agentarchive-portable.tar": "portable",
            "historical-evidence.tar": "legacy",
        }
        stats = {member: tree_stats(snapshot / member) for member in members.values()}
        for filename, member in members.items():
            progress(f"writing {filename} without compression")
            create_tar(snapshot, member, payload / filename)

    progress("hashing SSD payloads for end-to-end verification")
    archives = []
    checksum_lines = []
    for archive in sorted(
        path for path in payload.glob("*.tar") if not path.name.startswith("._")
    ):
        digest = sha256_file(archive)
        archives.append(
            {"name": archive.name, "bytes": archive.stat().st_size, "sha256": digest}
        )
        checksum_lines.append(f"{digest}  payload/{archive.name}")
    manifest = {
        "schema_version": 1,
        "seed_id": seed_id,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "purpose": "Mac AgentArchive catalog and raw evidence for WSL",
        "source_mutation": "none; all source data was copied",
        "wsl_raw_destination": "normal ~/.claude and ~/.codex history subdirectories",
        "wsl_mutable_state_import": False,
        "privacy": privacy,
        "trees": stats,
        "archives": archives,
    }
    (partial / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    for name in ("install_wsl_seed.sh", "SSD_README.md"):
        copy_file(REPO / "scripts" / name, partial / name)
    tools = partial / "tools"
    tools.mkdir()
    copy_file(REPO / "scripts/configure_syncthing.py", tools / "configure_syncthing.py")
    copy_file(REPO / "privacy_filter.py", tools / "privacy_filter.py")
    os.chmod(partial / "install_wsl_seed.sh", 0o700)
    for relative in (
        "install_wsl_seed.sh",
        "SSD_README.md",
        "manifest.json",
        "tools/configure_syncthing.py",
        "tools/privacy_filter.py",
    ):
        path = partial / relative
        checksum_lines.append(f"{sha256_file(path)}  {relative}")
    (partial / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    os.replace(partial, final)
    progress(f"complete: {final}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ssd-seed: failed without touching source data: {exc}", file=sys.stderr)
        raise SystemExit(1)
