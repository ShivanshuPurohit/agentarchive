#!/usr/bin/python3
"""Durable, incremental archive for local Claude Code and Codex history.

Raw harness state is staged with APFS copy-on-write clones, inventoried with
SHA-256 provenance, and stored in encrypted restic snapshots.  SQLite files
are captured with the online backup API.  A small catalog records every path,
change, deletion, session, and health check.  Generated indexes are explicitly
rebuildable; the restic repository and capture manifests are authoritative.

This program intentionally uses only the Python standard library plus restic.
It is designed to run unattended from launchd on macOS.
"""

from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import ctypes
import datetime as dt
import fcntl
import getpass
import gzip
import hashlib
import json
import os
import pathlib
import plistlib
import re
import secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import reasoning_index
from privacy_filter import transcript_is_excluded, workspace_is_excluded

from agentarchive.common import (
    COMMIT_RE, DEFAULT_CONFIG, DEFAULT_SOURCE_ROOT, DEFAULT_STATE, DURABLE_CAPTURE_STATUSES,
    GITHUB_ISSUE_RE, GITHUB_PR_RE, HOME, KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, LABEL_CAPTURE,
    LABEL_VERIFY, SUCCESSFUL_CAPTURE_STATUSES, URL_RE, UUID_RE, VERSION, atomic_write,
    capture_id, compact, ensure_private_tree, md_cell, progress, read_json, run, sha256_file,
    sha256_text, utc_now, write_json,
)
from agentarchive.config import default_config, load_config, state_root
from agentarchive.catalog import (
    backup_catalog_to_stage, catalog_path, connect_catalog, connect_catalog_readonly,
    latest_success, make_schema, record_health, record_item,
)
from agentarchive.storage import (
    backup_stage, clear_sqlite_temp_sidecars, copy_clone, device_identity,
    ensure_keychain_password, ensure_repository, exclusive_lock, keychain_has_password,
    known_restic_snapshot_ids, latest_restic_snapshot_id, parse_restic_snapshot, restic,
    restic_env, restic_repository_check, restic_repository_config, restic_snapshot_inventory,
    snapshot_for_capture, sqlite_backup, verify_snapshot_sentinel,
)
from agentarchive.staging import (
    inventory_stage, pending_inbox_items, persist_inventory, prior_files, remove_captured_inbox,
    stage_sources, write_manifest,
)
from agentarchive.metadata import native_macos_xattrs, path_xattrs, write_platform_metadata
from agentarchive.audit import (
    archived_file_indexes, audit_incomplete_stage, audit_staging, physical_entry_fingerprint,
    public_staging_audit, staging_candidates, summarize_staging_audit, write_staging_audit,
)
from agentarchive.recovery import (
    ensure_orphan_capture_row, persist_orphan_inventory, read_saved_orphan_audit,
    recover_incomplete_stages, validate_existing_stage_metadata, write_orphan_stage_metadata,
    write_saved_orphan_audit,
)
from agentarchive.replica import (
    raw_replica_definitions, raw_replica_inventory, verify_raw_replica_manifest,
    write_raw_replica_manifest,
)
from agentarchive.reports import (
    generate_capsule, index_legacy_documents, render_start_here, render_status,
    source_quick_counts,
)
from agentarchive.capture import backup_archive, capture, import_legacy, verify_archive
from agentarchive.indexing import (
    after_cutoff, extract_event, index_capture, indexable_jsonl, insert_reference,
    parse_timestamp, path_session_id, prefix_sha256, purge_excluded_sessions, text_from_content,
)
from agentarchive.search import (
    balanced_search_results, fts_query_variants, legacy_claude_raw_path, legacy_codex_raw_path,
    normalized_fts_query, search_archive, terse_search_coordinate, coverage_warnings,
)
from agentarchive.context import indexed_context
from agentarchive.dedup import apply_cow_dedup, dedup_priority, duplicate_report
from agentarchive.install import activate_launchd, install, launch_plists, merge_claude_hook
from agentarchive.schedule import enqueue_hook, power_eligible, run_scheduled, run_verify_scheduled
from agentarchive.offsite import configure_offsite


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install", help="install code, config, hook, repository, and launchd plists")
    install_parser.add_argument("--activate", action="store_true")
    sub.add_parser("activate", help="activate or reload launchd jobs")
    backup_parser = sub.add_parser(
        "backup",
        help="capture both harnesses now, then verify the repository and restore sentinel",
    )
    backup_parser.add_argument("--reason", default="manual-user-request")
    capture_parser = sub.add_parser("capture", help="capture both harnesses now")
    capture_parser.add_argument("--reason", default="manual")
    replica_parser = sub.add_parser(
        "replica-manifest",
        help="hash the complete portable raw-rollout trees before seeding or synchronization",
    )
    replica_parser.add_argument(
        "--acknowledge-excluded-history",
        action="store_true",
        help="explicitly permit excluded-workspace rollouts in the cross-device replica",
    )
    replica_verify_parser = sub.add_parser(
        "replica-verify",
        help="prove the current portable raw-rollout trees match a seed manifest",
    )
    replica_verify_parser.add_argument("manifest")
    replica_verify_parser.add_argument(
        "--acknowledge-excluded-history",
        action="store_true",
        help="explicitly permit excluded-workspace rollouts in the verification scope",
    )
    sub.add_parser("run-scheduled", help="launchd entry point")
    sub.add_parser("run-verify-scheduled", help="battery-aware weekly verification entry point")
    sub.add_parser("hook", help="fast Claude SessionEnd hook entry point")
    verify_parser = sub.add_parser("verify", help="verify catalog, repository, and a real restored sentinel")
    verify_parser.add_argument("--read-data", action="store_true")
    sub.add_parser("status", help="show archive health and coverage")
    compact_parser = sub.add_parser("compact", help="compact redundant indexes and verified inventory copies")
    compact_parser.add_argument("--apply", action="store_true", help="apply the verified compaction; default is preview")
    import_parser = sub.add_parser("import-legacy", help="archive both existing analysis workspaces before cleanup")
    import_parser.add_argument("--name", default="legacy-workspaces-pre-dedup")
    search_parser = sub.add_parser("search", help="search historical and incremental evidence")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=8)
    search_parser.add_argument("--json", action="store_true")
    reasoning_parser = sub.add_parser(
        "reasoning", help="search readable Claude/Codex reasoning without tool-output noise"
    )
    reasoning_parser.add_argument("query")
    reasoning_parser.add_argument("--limit", type=int, default=8)
    reasoning_parser.add_argument("--json", action="store_true")
    serve_parser = sub.add_parser(
        "serve", aliases=["browse"], help="open the foreground localhost transcript and reasoning viewer"
    )
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.add_argument("--no-open", action="store_true")
    context_parser = sub.add_parser(
        "context", help="expand one search hit ID or incremental PATH:LINE under a character budget"
    )
    context_parser.add_argument("coordinate")
    context_parser.add_argument("--before", type=int, default=0)
    context_parser.add_argument("--after", type=int, default=0)
    context_parser.add_argument("--max-chars", type=int, default=6000)
    report_parser = sub.add_parser("dedup-report", help="find exact duplicate workspace files")
    report_parser.add_argument("--minimum-size", type=int, default=1024 * 1024)
    apply_parser = sub.add_parser("dedup-apply", help="replace reported duplicates with safe APFS COW clones")
    apply_parser.add_argument("--report", default="")
    sub.add_parser(
        "staging-audit",
        help="hash abandoned staging trees and prove which contents already exist in durable snapshots",
    )
    cleanup_parser = sub.add_parser(
        "staging-cleanup",
        help="archive, verify, and remove incomplete abandoned staging trees",
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform recovery and deletion; without this flag the command is an audit-only dry run",
    )
    cleanup_parser.add_argument(
        "--read-data",
        action="store_true",
        help="read and verify every repository blob before deleting staging trees",
    )
    offsite_parser = sub.add_parser(
        "configure-offsite",
        help="transactionally initialize, migrate, and verify a second repository",
    )
    offsite_parser.add_argument("repository")
    offsite_parser.add_argument("--password-command")
    offsite_parser.add_argument(
        "--future-only",
        action="store_true",
        help="do not copy existing local snapshots (future captures only)",
    )
    offsite_parser.add_argument(
        "--acknowledge-excluded-history",
        action="store_true",
        help="explicitly permit migration of snapshots that may retain excluded-workspace metadata",
    )
    restore_parser = sub.add_parser("restore", help="restore a restic snapshot")
    restore_parser.add_argument("snapshot")
    restore_parser.add_argument("target")
    receipts_parser = sub.add_parser(
        "receipts", help="read deleted transcripts' model-call receipts back from the snapshots that still hold them"
    )
    receipts_parser.add_argument("--dry-run", action="store_true", help="count what the snapshots hold, restoring nothing")
    record_parser = sub.add_parser("record", help="add a curated decision/failure/outcome record")
    record_parser.add_argument("--kind", required=True, choices=("decision", "failure", "unresolved", "outcome", "supersession"))
    record_parser.add_argument("--title", required=True)
    record_parser.add_argument("--project")
    record_parser.add_argument("--status")
    record_parser.add_argument("--body")
    record_parser.add_argument("--harness", choices=("claude", "codex"))
    record_parser.add_argument("--session")
    record_parser.add_argument("--evidence-query")
    record_parser.add_argument("--supersedes")
    args = parser.parse_args(argv)
    config_path = pathlib.Path(args.config).expanduser().resolve()

    if args.command == "install":
        result = install(config_path, activate=args.activate)
    elif args.command == "activate":
        activate_launchd(config=load_config(config_path))
        result = {"activated": True}
    else:
        config = load_config(config_path)
        if args.command == "backup":
            with exclusive_lock(config):
                result = backup_archive(config, args.reason)
        elif args.command == "capture":
            with exclusive_lock(config):
                result = capture(config, args.reason)
        elif args.command == "replica-manifest":
            with exclusive_lock(config):
                result = write_raw_replica_manifest(
                    config,
                    acknowledge_excluded_history=args.acknowledge_excluded_history,
                )
        elif args.command == "replica-verify":
            with exclusive_lock(config):
                result = verify_raw_replica_manifest(
                    config,
                    pathlib.Path(args.manifest).expanduser().resolve(),
                    acknowledge_excluded_history=args.acknowledge_excluded_history,
                )
        elif args.command == "run-scheduled":
            result = run_scheduled(config)
        elif args.command == "run-verify-scheduled":
            result = run_verify_scheduled(config)
        elif args.command == "hook":
            result = enqueue_hook(config)
        elif args.command == "verify":
            with exclusive_lock(config):
                result = verify_archive(config, args.read_data)
        elif args.command == "status":
            conn = connect_catalog(config)
            try:
                render_status(config, conn)
            finally:
                conn.close()
            print((state_root(config) / "STATUS.md").read_text(encoding="utf-8"))
            return 0
        elif args.command == "import-legacy":
            with exclusive_lock(config):
                result = import_legacy(config, args.name)
        elif args.command == "compact":
            from agentarchive.maintenance import compact_catalog
            with exclusive_lock(config):
                result = compact_catalog(config, args.apply)
        elif args.command in ("search", "reasoning"):
            for warning in coverage_warnings(config):
                print(f"Archive coverage warning: {warning}", file=sys.stderr)
            found = search_archive(
                config, args.query, args.limit, reasoning_only=args.command == "reasoning"
            )
            if args.json:
                print(json.dumps(found, ensure_ascii=False, indent=2))
            else:
                for index, item in enumerate(found, 1):
                    coordinate = terse_search_coordinate(item)
                    hit = f" {item['hit_id']}" if item.get("hit_id") else ""
                    print(
                        f"{index}. [{item.get('match_mode','?')} "
                        f"{item.get('harness','?')}/{item.get('source','?')}{hit}] "
                        f"{coordinate}: {item.get('excerpt','')}"
                    )
            return 0
        elif args.command in {"serve", "browse"}:
            if not 0 <= args.port <= 65535:
                parser.error("--port must be between 0 and 65535")
            from transcript_viewer import serve
            return serve(config, search_archive, args.port, not args.no_open)
        elif args.command == "context":
            try:
                sys.stdout.write(
                    indexed_context(config, args.coordinate, args.before, args.after, args.max_chars)
                )
            except ValueError as exc:
                print(f"context error: {exc}", file=sys.stderr)
                return 2
            return 0
        elif args.command == "dedup-report":
            with exclusive_lock(config):
                destination = duplicate_report(config, args.minimum_size)
            result = read_json(destination)
        elif args.command == "dedup-apply":
            report = pathlib.Path(args.report).expanduser().resolve() if args.report else state_root(config) / "dedup-report.json"
            with exclusive_lock(config):
                result = apply_cow_dedup(config, report)
        elif args.command == "staging-audit":
            with exclusive_lock(config):
                ensure_private_tree(state_root(config))
                conn = connect_catalog(config)
                try:
                    result = public_staging_audit(audit_staging(config, conn))
                finally:
                    conn.close()
        elif args.command == "staging-cleanup":
            with exclusive_lock(config):
                ensure_private_tree(state_root(config))
                conn = connect_catalog(config)
                try:
                    if args.apply:
                        result = recover_incomplete_stages(
                            config,
                            conn,
                            read_data=args.read_data,
                        )
                    else:
                        result = {
                            "dry_run": True,
                            **public_staging_audit(audit_staging(config, conn)),
                        }
                finally:
                    conn.close()
        elif args.command == "configure-offsite":
            result = configure_offsite(
                config_path,
                args.repository,
                args.password_command,
                migrate_existing=not args.future_only,
                acknowledge_excluded_history=args.acknowledge_excluded_history,
            )
        elif args.command == "restore":
            pathlib.Path(args.target).mkdir(parents=True, exist_ok=True)
            restored = restic(config, ["restore", args.snapshot, "--target", args.target])
            result = {"snapshot": args.snapshot, "target": args.target, "output": restored.stdout}
        elif args.command == "receipts":
            from agentarchive.receipts import backfill
            with exclusive_lock(config):
                conn = connect_catalog(config)
                try:
                    result = backfill(config, conn, dry_run=args.dry_run)
                finally:
                    conn.close()
        elif args.command == "record":
            result = record_item(config, args)
        else:
            parser.error(f"unhandled command: {args.command}")
            return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command in ("backup", "verify") and result.get("status") == "failed":
        return 1
    if args.command == "replica-verify" and result.get("status") != "identical":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
