"""SQLite catalog schema, connections, health records, and consistent backups."""

from __future__ import annotations

import argparse
import os
import pathlib
import reasoning_index
import sqlite3
import sys
import time
import uuid
from typing import Any, Dict, Optional

from . import common as utils, config as configuration


BLOCKS_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
    text,harness UNINDEXED,session_id UNINDEXED,source_relpath UNINDEXED,
    line_no UNINDEXED,block_id UNINDEXED,role UNINDEXED,kind UNINDEXED,tool_name UNINDEXED,
    content='blocks',content_rowid='block_id',tokenize='unicode61'
)
"""


def catalog_path(config: Dict[str, Any]) -> pathlib.Path:
    return configuration.state_root(config) / "catalog.sqlite"


def connect_catalog(config: Dict[str, Any]) -> sqlite3.Connection:
    path = catalog_path(config)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    make_schema(conn)
    conn.commit()
    if path.exists():
        os.chmod(path, 0o600)
    return conn


def connect_catalog_readonly(config: Dict[str, Any]) -> sqlite3.Connection:
    """Open the catalog for queries without WAL/schema/chmod side effects."""
    return open_readonly(catalog_path(config))


def open_readonly(path: pathlib.Path) -> sqlite3.Connection:
    """Read a catalog or restored historical index, including checkpointed WAL files."""
    path = path.expanduser().resolve()
    for attempt in range(3):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.OperationalError:
            if conn is not None:
                conn.close()
            if attempt < 2:
                time.sleep(0.05 * (attempt + 1))
    # Some macOS SQLite builds intermittently fail to create the transient
    # read-only WAL shared-memory handle immediately after a clean checkpoint.
    # Immutable mode is safe only when no uncheckpointed sidecar exists.
    if not pathlib.Path(str(path) + "-wal").exists() and not pathlib.Path(str(path) + "-journal").exists():
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    raise sqlite3.OperationalError(f"unable to open read-only catalog: {path}")


def make_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS captures(
            capture_id TEXT PRIMARY KEY,
            started_utc TEXT NOT NULL,
            finished_utc TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            restic_snapshot TEXT,
            offsite_snapshot TEXT,
            file_count INTEGER DEFAULT 0,
            logical_bytes INTEGER DEFAULT 0,
            new_files INTEGER DEFAULT 0,
            changed_files INTEGER DEFAULT 0,
            unchanged_files INTEGER DEFAULT 0,
            deleted_files INTEGER DEFAULT 0,
            manifest_sha256 TEXT,
            warnings_json TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS files(
            capture_id TEXT NOT NULL REFERENCES captures(capture_id),
            harness TEXT NOT NULL,
            relpath TEXT NOT NULL,
            file_type TEXT NOT NULL,
            size_bytes INTEGER,
            mtime_ns INTEGER,
            mode INTEGER,
            uid INTEGER,
            gid INTEGER,
            sha256 TEXT,
            link_target TEXT,
            change_kind TEXT NOT NULL,
            PRIMARY KEY(capture_id,harness,relpath)
        );
        CREATE INDEX IF NOT EXISTS files_hash_idx ON files(sha256,size_bytes);
        CREATE INDEX IF NOT EXISTS files_path_idx ON files(harness,relpath,capture_id);
        CREATE TABLE IF NOT EXISTS latest_files(
            harness TEXT NOT NULL,
            relpath TEXT NOT NULL,
            file_type TEXT NOT NULL,
            size_bytes INTEGER,
            mtime_ns INTEGER,
            mode INTEGER,
            uid INTEGER,
            gid INTEGER,
            sha256 TEXT,
            link_target TEXT,
            present INTEGER NOT NULL,
            first_capture TEXT NOT NULL,
            last_capture TEXT NOT NULL,
            PRIMARY KEY(harness,relpath)
        );
        CREATE TABLE IF NOT EXISTS health_checks(
            check_id INTEGER PRIMARY KEY,
            checked_utc TEXT NOT NULL,
            check_type TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS legacy_backups(
            name TEXT PRIMARY KEY,
            created_utc TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            paths_json TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS indexed_files(
            harness TEXT NOT NULL,
            relpath TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            byte_offset INTEGER NOT NULL,
            line_no INTEGER NOT NULL,
            prefix_sha256 TEXT,
            last_capture TEXT NOT NULL,
            tool_calls_version INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(harness,relpath)
        );
        CREATE TABLE IF NOT EXISTS sessions(
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            parent_id TEXT,
            root_id TEXT,
            cwd TEXT,
            model TEXT,
            started_utc TEXT,
            ended_utc TEXT,
            first_prompt TEXT,
            latest_user TEXT,
            latest_assistant TEXT,
            tool_counts_json TEXT,
            source_relpath TEXT,
            source_sha256 TEXT,
            updated_capture TEXT NOT NULL,
            capsule_path TEXT,
            PRIMARY KEY(harness,session_id)
        );
        CREATE INDEX IF NOT EXISTS sessions_cwd_idx ON sessions(cwd);
        CREATE INDEX IF NOT EXISTS sessions_time_idx ON sessions(harness,started_utc,ended_utc);
        CREATE TABLE IF NOT EXISTS blocks(
            block_id INTEGER PRIMARY KEY,
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_relpath TEXT NOT NULL,
            line_no INTEGER NOT NULL,
            block_index INTEGER NOT NULL,
            timestamp TEXT,
            role TEXT,
            kind TEXT,
            tool_name TEXT,
            text TEXT NOT NULL,
            text_sha256 TEXT NOT NULL,
            capture_id TEXT NOT NULL,
            UNIQUE(harness,source_relpath,line_no,block_index,text_sha256)
        );
        CREATE INDEX IF NOT EXISTS blocks_session_idx ON blocks(harness,session_id,timestamp);
        CREATE TABLE IF NOT EXISTS references_found(
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            first_block_id INTEGER,
            last_seen_capture TEXT NOT NULL,
            PRIMARY KEY(harness,session_id,kind,value)
        );
        CREATE TABLE IF NOT EXISTS records(
            record_id TEXT PRIMARY KEY,
            project TEXT,
            kind TEXT NOT NULL CHECK(kind IN ('decision','failure','unresolved','outcome','supersession')),
            status TEXT,
            title TEXT NOT NULL,
            body TEXT,
            source_harness TEXT,
            source_session_id TEXT,
            evidence_query TEXT,
            supersedes_record_id TEXT,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents(
            document_id INTEGER PRIMARY KEY,
            harness TEXT,
            path TEXT UNIQUE NOT NULL,
            title TEXT,
            sha256 TEXT NOT NULL,
            text TEXT NOT NULL,
            indexed_utc TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            text,
            title,
            path UNINDEXED,
            harness UNINDEXED,
            document_id UNINDEXED,
            tokenize='unicode61'
        );
        CREATE TABLE IF NOT EXISTS summary_jobs(
            harness TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_sha256 TEXT,
            generator_version TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            queued_utc TEXT NOT NULL,
            completed_utc TEXT,
            PRIMARY KEY(harness,session_id,source_sha256,generator_version)
        );
        """
    )
    conn.execute(BLOCKS_FTS_SQL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(indexed_files)")}
    if "tool_calls_version" not in columns:
        conn.execute(
            "ALTER TABLE indexed_files ADD COLUMN tool_calls_version INTEGER NOT NULL DEFAULT 0"
        )
    reasoning_index.ensure_schema(conn)


def _clone_checkpointed_catalog(conn: sqlite3.Connection, destination: pathlib.Path) -> bool:
    """Clone only while a writer lock proves the main file is complete."""
    if sys.platform != "darwin" or conn.in_transaction:
        return False
    source_name = conn.execute("PRAGMA database_list").fetchone()[2]
    if not source_name:
        return False
    source = pathlib.Path(source_name)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    locked = False
    try:
        # A long-lived reader/writer must select the online-backup fallback,
        # not turn this optimization into an extra 60-second wait.
        conn.execute("PRAGMA busy_timeout=0")
        if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            return False
        conn.execute("BEGIN IMMEDIATE")
        locked = True
        wal = pathlib.Path(str(source) + "-wal")
        if wal.exists() and wal.stat().st_size:
            return False  # A writer committed between checkpoint and lock.
        copied = utils.run(["/bin/cp", "-c", str(source), str(destination)], check=False)
        if copied.returncode == 0 and destination.is_file():
            return True
    except (OSError, sqlite3.OperationalError):
        pass
    finally:
        if locked:
            conn.rollback()
        conn.execute(f"PRAGMA busy_timeout={int(timeout)}")
    if destination.is_file():
        destination.unlink()  # Only an incomplete generated clone.
    return False


def validate_catalog_snapshot(path: pathlib.Path) -> None:
    backup = open_readonly(path)
    try:
        result = backup.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"catalog backup quick_check returned {result}")
    finally:
        backup.close()


def backup_catalog_to_stage(conn: sqlite3.Connection, stage: pathlib.Path, *, validate: bool = True) -> Dict[str, Any]:
    if conn.in_transaction:
        raise RuntimeError("commit catalog changes before taking its recovery snapshot")
    destination = stage / "meta" / "catalog.sqlite"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cloned = _clone_checkpointed_catalog(conn, destination)
    backup = sqlite3.connect(str(destination))
    try:
        if not cloned:
            conn.backup(backup)
        backup.execute("PRAGMA journal_mode=DELETE")
    finally:
        backup.close()
    os.chmod(destination, 0o600)
    if validate:
        validate_catalog_snapshot(destination)
    return {"method": "checkpointed-apfs-clone" if cloned else "sqlite-online-backup",
            "integrity": "ok" if validate else "pending", "bytes": destination.stat().st_size}


def record_health(conn: sqlite3.Connection, kind: str, status: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO health_checks(checked_utc,check_type,status,detail) VALUES(?,?,?,?)",
        (utils.utc_now(), kind, status, detail),
    )
    conn.commit()


def latest_success(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM captures WHERE status IN ('complete','local_complete_offsite_failed') ORDER BY started_utc DESC LIMIT 1"
    ).fetchone()


def record_item(config: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    conn = connect_catalog(config)
    try:
        identifier = str(uuid.uuid4())
        now = utils.utc_now()
        conn.execute(
            """
            INSERT INTO records(record_id,project,kind,status,title,body,source_harness,
                                source_session_id,evidence_query,supersedes_record_id,created_utc,updated_utc)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                identifier,
                args.project,
                args.kind,
                args.status,
                args.title,
                args.body,
                args.harness,
                args.session,
                args.evidence_query,
                args.supersedes,
                now,
                now,
            ),
        )
        conn.commit()
        return {"record_id": identifier, "kind": args.kind, "title": args.title}
    finally:
        conn.close()
