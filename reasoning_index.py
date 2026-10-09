#!/usr/bin/env python3
"""Normalized, provenance-preserving index for reasoning absent from the main catalog.

The raw JSONL remains authoritative.  Reasoning text is stored once per SHA-256
payload while every source occurrence keeps its session, file, line, timestamp,
kind, and capture identity.  FTS5 uses the payload table as external content, so
search does not create another shadow copy of the text.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple


UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
KINDS = ("agent_reasoning", "reasoning_content", "reasoning_summary")


SCHEMA = """
CREATE TABLE IF NOT EXISTS reasoning_payloads(
    payload_id INTEGER PRIMARY KEY,
    text_sha256 TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS reasoning_fts USING fts5(
    text,
    content='reasoning_payloads',
    content_rowid='payload_id',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS reasoning_payloads_ai AFTER INSERT ON reasoning_payloads BEGIN
    INSERT INTO reasoning_fts(rowid,text) VALUES(new.payload_id,new.text);
END;
CREATE TABLE IF NOT EXISTS reasoning_occurrences(
    occurrence_id INTEGER PRIMARY KEY,
    payload_id INTEGER NOT NULL REFERENCES reasoning_payloads(payload_id),
    harness TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_relpath TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    timestamp TEXT,
    kind TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    UNIQUE(harness,source_relpath,line_no,kind,payload_id)
);
CREATE INDEX IF NOT EXISTS reasoning_occurrence_payload
    ON reasoning_occurrences(payload_id,occurrence_id);
CREATE INDEX IF NOT EXISTS reasoning_occurrence_session
    ON reasoning_occurrences(harness,session_id,line_no);
CREATE TABLE IF NOT EXISTS reasoning_scan_state(
    harness TEXT NOT NULL,
    relpath TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    byte_offset INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    prefix_sha256 TEXT NOT NULL,
    owner_session_id TEXT NOT NULL,
    opaque_items INTEGER NOT NULL DEFAULT 0,
    opaque_bytes INTEGER NOT NULL DEFAULT 0,
    last_capture TEXT NOT NULL,
    PRIMARY KEY(harness,relpath)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _prefix_sha256(path: pathlib.Path, amount: int = 65536) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(amount))
    return digest.hexdigest()


def _fallback_session_id(relpath: str) -> str:
    matches = UUID_RE.findall(relpath)
    return matches[-1].lower() if matches else pathlib.Path(relpath).stem


def _owner_identity(path: pathlib.Path, relpath: str) -> Tuple[str, Optional[str]]:
    """Return the file owner from its first header, never an embedded parent header."""
    fallback = _fallback_session_id(relpath)
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                return fallback, None
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "session_meta":
                value = payload.get("id")
                owner = value.lower() if isinstance(value, str) and UUID_RE.fullmatch(value) else fallback
                parent = payload.get("parent_thread_id") or payload.get("forked_from_id")
                return owner, parent if isinstance(parent, str) else None
            value = obj.get("id")
            if isinstance(value, str) and UUID_RE.fullmatch(value):
                return value.lower(), None
            return fallback, None
    return fallback, None


def _plain_text(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        result: List[str] = []
        for item in value:
            result.extend(_plain_text(item))
        return result
    if not isinstance(value, dict):
        return []
    result = []
    for key in ("text", "thinking", "output_text", "input_text"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            result.append(text)
    for key in ("content", "summary"):
        if key in value:
            result.extend(_plain_text(value[key]))
    return result


def extract_reasoning(obj: Dict[str, Any]) -> Tuple[List[Tuple[str, str, Optional[str]]], int]:
    """Return readable reasoning and the opaque encrypted byte count for one event."""
    top_type = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
    data = payload if payload is not None else obj
    inner = data.get("type") if isinstance(data, dict) else None
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, str):
        timestamp = None
    found: List[Tuple[str, str, Optional[str]]] = []
    opaque = 0

    if top_type == "event_msg" and inner == "agent_reasoning":
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            found.append(("agent_reasoning", text, timestamp))
        return found, 0

    is_reasoning = (
        (top_type == "response_item" and inner == "reasoning")
        or (payload is None and top_type == "reasoning")
    )
    if not is_reasoning:
        return found, 0

    for text in _plain_text(data.get("summary")):
        found.append(("reasoning_summary", text, timestamp))
    for text in _plain_text(data.get("content")):
        found.append(("reasoning_content", text, timestamp))
    encrypted = data.get("encrypted_content")
    if isinstance(encrypted, str):
        opaque = len(encrypted)
    return found, opaque


def _scan_files(stage: pathlib.Path) -> Iterator[Tuple[pathlib.Path, str]]:
    root = stage / "data" / "codex"
    for subdir in ("sessions", "archived_sessions"):
        base = root / subdir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            if path.is_file() and ".sync-conflict-" not in path.name:
                yield path, path.relative_to(root).as_posix()


def _payload_id(conn: sqlite3.Connection, text: str) -> Tuple[int, bool]:
    digest = _sha256_text(text)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO reasoning_payloads(text_sha256,text) VALUES(?,?)",
        (digest, text),
    )
    row = conn.execute(
        "SELECT payload_id FROM reasoning_payloads WHERE text_sha256=?", (digest,)
    ).fetchone()
    return int(row[0]), cursor.rowcount == 1


def index_stage(conn: sqlite3.Connection, stage: pathlib.Path, capture_id: str) -> Dict[str, int]:
    """Incrementally index Codex readable reasoning from one capture stage."""
    ensure_schema(conn)
    stats = {
        "files": 0, "indexed": 0, "updated": 0, "skipped": 0,
        "payloads": 0, "occurrences": 0, "opaque_items": 0, "opaque_bytes": 0,
    }
    for path, relpath in _scan_files(stage):
        stats["files"] += 1
        info = path.stat()
        prefix = _prefix_sha256(path)
        prior = conn.execute(
            "SELECT * FROM reasoning_scan_state WHERE harness='codex' AND relpath=?",
            (relpath,),
        ).fetchone()
        if prior and prior["size_bytes"] == info.st_size and prior["mtime_ns"] == info.st_mtime_ns:
            stats["skipped"] += 1
            continue

        owner, _parent = _owner_identity(path, relpath)
        offset = 0
        line_no = 0
        opaque_items = 0
        opaque_bytes = 0
        if prior and prior["prefix_sha256"] == prefix and info.st_size >= prior["byte_offset"]:
            offset = int(prior["byte_offset"])
            line_no = int(prior["line_no"])
            opaque_items = int(prior["opaque_items"])
            opaque_bytes = int(prior["opaque_bytes"])
            stats["updated"] += 1
        else:
            stats["indexed"] += 1

        safe_offset = offset
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_start = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    safe_offset = line_start
                    break
                safe_offset = handle.tell()
                line_no += 1
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                entries, encrypted_bytes = extract_reasoning(obj)
                if encrypted_bytes:
                    opaque_items += 1
                    opaque_bytes += encrypted_bytes
                    stats["opaque_items"] += 1
                    stats["opaque_bytes"] += encrypted_bytes
                for kind, text, timestamp in entries:
                    payload_id, created = _payload_id(conn, text)
                    stats["payloads"] += int(created)
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO reasoning_occurrences(
                            payload_id,harness,session_id,source_relpath,line_no,
                            timestamp,kind,capture_id
                        ) VALUES(?,'codex',?,?,?,?,?,?)
                        """,
                        (payload_id, owner, relpath, line_no, timestamp, kind, capture_id),
                    )
                    stats["occurrences"] += int(cursor.rowcount == 1)

        conn.execute(
            """
            INSERT INTO reasoning_scan_state(
                harness,relpath,size_bytes,mtime_ns,byte_offset,line_no,prefix_sha256,
                owner_session_id,opaque_items,opaque_bytes,last_capture
            ) VALUES('codex',?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(harness,relpath) DO UPDATE SET
                size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                byte_offset=excluded.byte_offset,line_no=excluded.line_no,
                prefix_sha256=excluded.prefix_sha256,
                owner_session_id=excluded.owner_session_id,
                opaque_items=excluded.opaque_items,opaque_bytes=excluded.opaque_bytes,
                last_capture=excluded.last_capture
            """,
            (
                relpath, info.st_size, info.st_mtime_ns, safe_offset, line_no, prefix,
                owner, opaque_items, opaque_bytes, capture_id,
            ),
        )
    return stats


def search(conn: sqlite3.Connection, match: str, limit: int) -> List[Dict[str, Any]]:
    """Return one canonical provenance coordinate per unique matching payload."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reasoning_fts'"
    ).fetchone():
        return []
    rows = conn.execute(
        """
        SELECT p.payload_id,o.session_id,o.source_relpath,o.line_no,o.timestamp,o.kind,
               snippet(reasoning_fts,0,'[',']','…',40) AS excerpt,
               rank AS bm25_score,p.text_sha256,
               length(p.text) AS _chars,substr(p.text,1,3000) AS _prefix,
               (SELECT COUNT(*) FROM reasoning_occurrences c
                WHERE c.payload_id=p.payload_id) AS occurrence_count
        FROM reasoning_fts
        JOIN reasoning_payloads p ON p.payload_id=reasoning_fts.rowid
        JOIN reasoning_occurrences o ON o.occurrence_id=(
            SELECT MIN(c.occurrence_id) FROM reasoning_occurrences c
            WHERE c.payload_id=p.payload_id
        )
        WHERE reasoning_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def context(conn: sqlite3.Connection, payload_id: int, occurrence_limit: int = 8) -> Optional[Dict[str, Any]]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reasoning_payloads'"
    ).fetchone():
        return None
    payload = conn.execute(
        "SELECT payload_id,text_sha256,text FROM reasoning_payloads WHERE payload_id=?",
        (payload_id,),
    ).fetchone()
    if not payload:
        return None
    total = conn.execute(
        "SELECT COUNT(*) FROM reasoning_occurrences WHERE payload_id=?", (payload_id,)
    ).fetchone()[0]
    occurrences = conn.execute(
        """
        SELECT harness,session_id,source_relpath,line_no,timestamp,kind,capture_id
        FROM reasoning_occurrences WHERE payload_id=?
        ORDER BY occurrence_id LIMIT ?
        """,
        (payload_id, occurrence_limit),
    ).fetchall()
    return {"payload": dict(payload), "occurrence_count": total,
            "occurrences": [dict(row) for row in occurrences]}


def stats(conn: sqlite3.Connection) -> Dict[str, int]:
    ensure_schema(conn)
    return {
        "payloads": conn.execute("SELECT COUNT(*) FROM reasoning_payloads").fetchone()[0],
        "occurrences": conn.execute("SELECT COUNT(*) FROM reasoning_occurrences").fetchone()[0],
        "files": conn.execute("SELECT COUNT(*) FROM reasoning_scan_state").fetchone()[0],
        "opaque_items": conn.execute(
            "SELECT COALESCE(SUM(opaque_items),0) FROM reasoning_scan_state"
        ).fetchone()[0],
        "opaque_bytes": conn.execute(
            "SELECT COALESCE(SUM(opaque_bytes),0) FROM reasoning_scan_state"
        ).fetchone()[0],
    }
