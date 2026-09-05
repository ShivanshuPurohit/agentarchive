#!/usr/bin/env python3
"""Version-retaining plaintext backup of device state through rclone.

Only changed files are uploaded. Replaced destination files are moved into a
timestamped versions tree; source deletions are never propagated. Each device
has its own namespace, so Mac and WSL machine state is preserved, not merged.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Tuple


SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{3,80}$")


def run(command: List[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "unknown command error").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {detail[:4000]}")
    return result


def source_stats(
    path: pathlib.Path,
    excluded_root_files: Tuple[str, ...] = (),
) -> Tuple[int, int]:
    files = 0
    bytes_total = 0
    for current, _dirs, names in os.walk(path, followlinks=False):
        root = pathlib.Path(current)
        for name in names:
            if root == path and name in excluded_root_files:
                continue
            candidate = root / name
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if candidate.is_file() or candidate.is_symlink():
                files += 1
                bytes_total += info.st_size
    return files, bytes_total


def parse_extra(values: List[str]) -> List[Tuple[str, pathlib.Path]]:
    result: List[Tuple[str, pathlib.Path]] = []
    for value in values:
        try:
            name, path_text = value.split("=", 1)
        except ValueError as exc:
            raise RuntimeError("--extra must use NAME=/absolute/path") from exc
        if not SAFE_NAME.fullmatch(name):
            raise RuntimeError(f"unsafe --extra name: {name!r}")
        result.append((name, pathlib.Path(path_text).expanduser().resolve()))
    return result


def remote_join(root: str, *parts: str) -> str:
    return root.rstrip("/") + "/" + "/".join(part.strip("/") for part in parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", required=True, help="plain rclone Drive path, e.g. drive:AgentArchive")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--claude-home", type=pathlib.Path, default=pathlib.Path.home() / ".claude")
    parser.add_argument("--codex-home", type=pathlib.Path, default=pathlib.Path.home() / ".codex")
    parser.add_argument("--extra", action="append", default=[], help="additional device tree as NAME=/path")
    parser.add_argument(
        "--portable-state",
        type=pathlib.Path,
        help="AgentArchivePortable tree; its rebuildable catalog is excluded from Drive",
    )
    parser.add_argument("--receipt-dir", type=pathlib.Path, default=pathlib.Path.home() / ".local/share/AgentArchive/cloud-receipts")
    parser.add_argument("--acknowledge-plaintext-credentials", action="store_true")
    parser.add_argument("--acknowledge-excluded-workspace-history", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if not SAFE_NAME.fullmatch(args.device_id):
        parser.error("--device-id must use only letters, digits, dot, underscore, or dash")
    if not args.acknowledge_plaintext_credentials:
        parser.error(
            "complete homes include auth tokens; pass --acknowledge-plaintext-credentials "
            "to store them without client-side encryption"
        )
    if not args.acknowledge_excluded_workspace_history:
        parser.error(
            "complete homes may contain excluded-workspace rollout data or "
            "metadata; explicit --acknowledge-excluded-workspace-history is required"
        )
    if ":" not in args.remote or args.remote.startswith(("crypt:", "rclone:crypt:")):
        parser.error("--remote must be a non-crypt rclone remote path")
    rclone = shutil.which("rclone")
    if not rclone:
        parser.error("rclone is not installed or absent from PATH")
    remote_name = args.remote.split(":", 1)[0]
    redacted = run([rclone, "config", "redacted", remote_name]).stdout
    if not re.search(r"(?m)^type\s*=\s*drive\s*$", redacted):
        parser.error("the selected rclone remote must be a plain Google Drive backend")

    sources = [
        ("claude-home", args.claude_home.expanduser().resolve(), ()),
        ("codex-home", args.codex_home.expanduser().resolve(), ()),
        *((name, path, ()) for name, path in parse_extra(args.extra)),
    ]
    if args.portable_state:
        sources.append(
            (
                "agentarchive-portable-metadata",
                args.portable_state.expanduser().resolve(),
                ("catalog.sqlite",),
            )
        )
    plan: List[Dict[str, object]] = []
    for name, path, excluded_root_files in sources:
        if not path.is_dir() or path == pathlib.Path(path.anchor):
            parser.error(f"source is not a safe existing directory: {path}")
        files, bytes_total = source_stats(path, excluded_root_files)
        if files == 0:
            parser.error(f"source contains no regular files or symlinks: {path}")
        plan.append(
            {
                "name": name,
                "path": str(path),
                "files": files,
                "bytes": bytes_total,
                "excluded_root_files": list(excluded_root_files),
            }
        )

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result: Dict[str, object] = {
        "status": "dry-run" if not args.apply else "complete",
        "created_utc": timestamp,
        "device_id": args.device_id,
        "remote": args.remote,
        "encryption": "none (plain Google Drive backend)",
        "sources": plan,
        "operations": [],
    }
    if args.apply:
        for item in plan:
            name = str(item["name"])
            destination = remote_join(
                args.remote, "devices", args.device_id, "current", name
            )
            versions = remote_join(
                args.remote, "devices", args.device_id, "versions", timestamp, name
            )
            filters = [
                value
                for excluded in item["excluded_root_files"]
                for value in ("--exclude", f"/{excluded}")
            ]
            copied = run(
                [
                    rclone,
                    "copy",
                    str(item["path"]),
                    destination,
                    "--backup-dir",
                    versions,
                    "--checksum",
                    "--links",
                    "--metadata",
                    "--create-empty-src-dirs",
                    "--fast-list",
                    "--checkers",
                    "8",
                    "--transfers",
                    "4",
                    "--stats-one-line",
                    "--stats",
                    "30s",
                    *filters,
                ]
            )
            checked = run(
                [
                    rclone,
                    "check",
                    str(item["path"]),
                    destination,
                    "--one-way",
                    "--links",
                    *filters,
                ]
            )
            result["operations"].append(
                {
                    "name": name,
                    "destination": destination,
                    "versions": versions,
                    "copy": (copied.stdout or copied.stderr).strip()[-2000:],
                    "check": (checked.stdout or checked.stderr).strip()[-2000:] or "ok",
                }
            )
        args.receipt_dir = args.receipt_dir.expanduser().resolve()
        args.receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = args.receipt_dir / f"plain-cloud-{args.device_id}-{timestamp}.json"
        temporary = receipt.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, receipt)
        result["receipt"] = str(receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"plain-cloud-backup: {exc}", file=sys.stderr)
        raise SystemExit(1)
