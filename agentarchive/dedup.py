"""Exact-content duplicate accounting and copy-on-write deduplication."""

from __future__ import annotations

import collections
import contextlib
import os
import pathlib
import uuid
from typing import Any, Dict, List, Tuple

from . import common as utils, config as configuration


def dedup_priority(path: pathlib.Path) -> Tuple[int, str]:
    text = str(path)
    if "/source_snapshot/" in text:
        return (0, text)
    if "/analysis/complete/" in text:
        return (1, text)
    if "/analysis/cutoff/" in text:
        return (2, text)
    return (3, text)


def duplicate_report(config: Dict[str, Any], minimum_size: int = 1024 * 1024) -> pathlib.Path:
    roots = [pathlib.Path(path).resolve() for path in config.get("legacy_workspaces", [])]
    by_size: Dict[int, List[pathlib.Path]] = collections.defaultdict(list)
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    size = path.stat().st_size
                    if size >= minimum_size:
                        by_size[size].append(path)
            except OSError:
                continue
    groups: List[Dict[str, Any]] = []
    for size, paths in sorted(by_size.items(), reverse=True):
        if len(paths) < 2:
            continue
        by_hash: Dict[str, List[pathlib.Path]] = collections.defaultdict(list)
        for path in paths:
            with contextlib.suppress(OSError):
                by_hash[utils.sha256_file(path)].append(path)
        for digest, identical in by_hash.items():
            if len(identical) < 2:
                continue
            ordered = sorted(identical, key=dedup_priority)
            groups.append(
                {
                    "sha256": digest,
                    "size_bytes": size,
                    "canonical": str(ordered[0]),
                    "duplicates": [str(path) for path in ordered[1:]],
                    "logical_redundant_bytes": size * (len(ordered) - 1),
                }
            )
    report = {
        "generated_utc": utils.utc_now(),
        "roots": [str(path) for path in roots],
        "minimum_size": minimum_size,
        "groups": groups,
        "duplicate_files": sum(len(group["duplicates"]) for group in groups),
        "logical_redundant_bytes": sum(group["logical_redundant_bytes"] for group in groups),
        "strategy": "Replace exact duplicate paths with APFS copy-on-write clones; paths remain valid and later writes diverge safely.",
    }
    destination = configuration.state_root(config) / "dedup-report.json"
    utils.write_json(destination, report)
    lines = [
        "# Existing exact-duplicate report",
        "",
        f"Generated: `{report['generated_utc']}`",
        "",
        f"- Duplicate files: **{report['duplicate_files']:,}**",
        f"- Logical redundant bytes: **{report['logical_redundant_bytes']:,}**",
        "- Planned action: path-preserving APFS copy-on-write deduplication after archive verification.",
        "",
        "| Size | Canonical | Duplicate paths |",
        "|---:|---|---|",
    ]
    for group in groups:
        lines.append(
            f"| {group['size_bytes']:,} | `{group['canonical']}` | {len(group['duplicates']):,} |"
        )
    utils.atomic_write(configuration.state_root(config) / "DEDUP_REPORT.md", ("\n".join(lines) + "\n").encode("utf-8"), 0o600)
    return destination


def apply_cow_dedup(config: Dict[str, Any], report_path: pathlib.Path) -> Dict[str, Any]:
    report = utils.read_json(report_path)
    if not isinstance(report, dict) or not isinstance(report.get("groups"), list):
        raise RuntimeError(f"invalid duplicate report: {report_path}")
    allowed_roots = [pathlib.Path(path).resolve() for path in config.get("legacy_workspaces", [])]
    replaced = 0
    logical = 0
    skipped: List[str] = []
    for group in report["groups"]:
        canonical = pathlib.Path(group["canonical"]).resolve()
        expected = str(group["sha256"])
        if not canonical.is_file() or utils.sha256_file(canonical) != expected:
            skipped.append(f"canonical changed: {canonical}")
            continue
        for value in group["duplicates"]:
            duplicate = pathlib.Path(value).resolve()
            if not any(duplicate == root or root in duplicate.parents for root in allowed_roots):
                skipped.append(f"outside allowed roots: {duplicate}")
                continue
            if not duplicate.is_file() or duplicate.is_symlink():
                skipped.append(f"not a regular file: {duplicate}")
                continue
            if utils.sha256_file(duplicate) != expected:
                skipped.append(f"content changed: {duplicate}")
                continue
            temp = duplicate.with_name(f".{duplicate.name}.cow-{os.getpid()}-{uuid.uuid4().hex}")
            result = utils.run(["/bin/cp", "-c", "-p", str(canonical), str(temp)], check=False)
            if result.returncode != 0 or not temp.is_file() or utils.sha256_file(temp) != expected:
                with contextlib.suppress(OSError):
                    temp.unlink()
                skipped.append(f"clone failed: {duplicate}: {utils.compact(result.stderr, 300)}")
                continue
            os.replace(temp, duplicate)
            replaced += 1
            logical += int(group["size_bytes"])
    result = {
        "completed_utc": utils.utc_now(),
        "paths_replaced_with_cow_clones": replaced,
        "logical_bytes_deduplicated": logical,
        "skipped": skipped,
    }
    utils.write_json(configuration.state_root(config) / "dedup-result.json", result)
    return result
