#!/usr/bin/env python3
"""Legacy experimental generated-token index for Claude Code and Codex.

Deprecated for production use. ``agent-archive reasoning/search/serve`` now
provides the useful retrieval and transcript-view behavior without copying raw
conversation bodies into a second multi-gigabyte database. This script remains
only as migration provenance and an explicitly gated research benchmark.

What it indexes, per turn item, in stable order:
  * Claude (`~/.claude/projects/**/*.jsonl`): every plaintext ``thinking`` block
    (the full chain of thought), assistant text, tool_use / tool_result pairs,
    user prompts, attachment injections, and compaction markers. The opaque
    ``thinking.signature`` verification token is stored as length only.
  * Codex (`~/.codex/sessions/**/rollout-*.jsonl`): the readable reasoning
    surfaces — ``reasoning.summary.summary_text`` and the streamed
    ``event_msg/agent_reasoning`` text — plus function/custom-tool calls and
    their structured results (``exec_command_end`` / ``patch_apply_end``), user
    and assistant messages, web/tool searches, and turn boundaries. The opaque
    ``encrypted_content`` Fernet blob is stored as a length+flag only and the
    item is honestly marked ``reasoning_encrypted``; it is never decoded.

Its historical database is rebuildable, not archival authority. Building it
duplicates content already retained in raw JSONL and AgentArchive FTS.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
import traceback
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

# Reuse a few pure helpers from the sibling agent_archive module so this tool's
# search semantics stay in lockstep with the existing catalog. Importing that
# module is safe: its only module-level work is constant assignment.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
try:
    from agent_archive import (  # type: ignore
        UUID_RE,
        balanced_search_results,
        compact,
        fts_query_variants,
        parse_timestamp,
        path_session_id,
        prefix_sha256,
        sha256_text,
    )
except Exception as exc:  # pragma: no cover - defensive fallback
    sys.stderr.write(f"agent_browse: could not import helpers from agent_archive: {exc}\n")
    raise

VERSION = "0.1.0"

HOME = pathlib.Path.home()
CLAUDE_ROOT = HOME / ".claude"
CODEX_ROOT = HOME / ".codex"
HARNESS_ROOTS = {"claude": CLAUDE_ROOT, "codex": CODEX_ROOT}
SCAN_SUBDIRS = {"claude": ("projects",), "codex": ("sessions", "archived_sessions")}

DEFAULT_DB = pathlib.Path(__file__).resolve().parent / "agent_browse.db"

# Granular item kinds. Reasoning that is readable vs opaque are distinct so the
# honesty layer can never imply encrypted CoT was recovered.
REASONING_KINDS = {"thinking", "reasoning_summary", "agent_reasoning"}
REASONING_OPAQUE_KIND = "reasoning_encrypted"
TOOL_CALL_KINDS = {"tool_use", "function_call", "custom_tool_call"}
TOOL_RESULT_KINDS = {
    "tool_result", "function_call_output", "custom_tool_call_output",
    "exec_end", "patch_end",
}

# Cap the text duplicated into the FTS5 shadow tables. Full content is always
# retained in items.content for `show` / replay; this only bounds tokenization
# cost and index size for multi-megabyte tool outputs.
FTS_TEXT_CAP = 65536

FLAT_CODEX_TYPES = {
    "message", "reasoning", "function_call", "function_call_output",
    "custom_tool_call", "custom_tool_call_output",
}
ENVELOPE_TYPES = {
    "session_meta", "response_item", "event_msg", "turn_context",
    "world_state", "compacted", "inter_agent_communication_metadata",
}

HIT_RE = re.compile(r"i:(claude|codex):([0-9]+)")
UUID_FULL_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_args_list(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or None


def flatten_text(value: Any) -> str:
    """Best-effort flattening of a message/tool content node to plain text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            parts.append(flatten_text(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("text", "thinking", "output_text", "input_text", "message"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text
        if value.get("type") == "image" or value.get("type") == "input_image":
            return "[image omitted]"
        # Generic: serialize without huge base64 payloads.
        return json.dumps(_scrub_b64(value), ensure_ascii=False)
    return str(value)


def _scrub_b64(obj: Any) -> Any:
    """Replace likely base64 blobs with a length marker for readable serialization."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and len(v) > 512 and re.fullmatch(r"[A-Za-z0-9+/=_]+", v):
                out[k] = f"<{len(v)} chars base64>"
            else:
                out[k] = _scrub_b64(v)
        return out
    if isinstance(obj, list):
        return [_scrub_b64(v) for v in obj]
    return obj


def first_str(values: Iterable[Any]) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def claude_tool_title(name: Optional[str], inp: Any) -> str:
    label = name or "tool"
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt", "note"):
            v = inp.get(key)
            if isinstance(v, str) and v.strip():
                return f"{label}: {v.strip()[:100]}"
            if isinstance(v, list):
                return f"{label}: {' '.join(str(x) for x in v)[:100]}"
    return label


def codex_call_title(name: Optional[str], args: Any) -> str:
    label = name or "tool"
    parsed: Any = args
    if isinstance(args, str):
        with contextlib.suppress(Exception):
            parsed = json.loads(args)
    if isinstance(parsed, dict):
        for key in ("command", "cmd", "path", "query", "name", "content"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                return f"{label}: {v.strip()[:100]}"
            if isinstance(v, list):
                return f"{label}: {' '.join(str(x) for x in v)[:100]}"
    return label


def cmd_title(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(x) for x in command)[:120]
    if isinstance(command, str):
        return command[:120]
    return ""


def clamp_ts(ts: Optional[str]) -> Optional[str]:
    if isinstance(ts, str) and ts.strip():
        return ts
    return None


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_state(
  harness TEXT NOT NULL,
  relpath TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  byte_offset INTEGER NOT NULL,
  line_no INTEGER NOT NULL,
  prefix_sha256 TEXT NOT NULL,
  meta_json TEXT,
  indexed_utc TEXT,
  PRIMARY KEY(harness, relpath)
);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT NOT NULL,
  harness TEXT NOT NULL,
  kind TEXT,
  project TEXT,
  cwd TEXT,
  git_branch TEXT,
  first_ts TEXT,
  last_ts TEXT,
  model TEXT,
  entrypoint TEXT,
  cli_version TEXT,
  parent_session_id TEXT,
  title TEXT,
  n_items INTEGER DEFAULT 0,
  has_encrypted_reasoning INTEGER DEFAULT 0,
  max_reasoning_tokens INTEGER,
  indexed_utc TEXT,
  PRIMARY KEY(harness, session_id)
);
CREATE TABLE IF NOT EXISTS items(
  item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT NOT NULL,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  line_no INTEGER NOT NULL,
  source_relpath TEXT NOT NULL,
  ts TEXT,
  role TEXT,
  kind TEXT NOT NULL,
  call_id TEXT,
  tool_name TEXT,
  display_title TEXT,
  content TEXT,
  content_sha256 TEXT NOT NULL,
  exit_code INTEGER,
  is_error INTEGER DEFAULT 0,
  meta_json TEXT,
  UNIQUE(harness, source_relpath, line_no, kind, content_sha256)
);
CREATE INDEX IF NOT EXISTS items_session_seq ON items(harness, session_id, seq, item_id);
CREATE INDEX IF NOT EXISTS items_call ON items(harness, session_id, call_id) WHERE call_id IS NOT NULL;
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  content,
  harness UNINDEXED,
  session_id UNINDEXED,
  kind UNINDEXED,
  role UNINDEXED,
  item_id UNINDEXED,
  tokenize='unicode61'
);
"""


def connect(db_path: pathlib.Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(
                f"database not found at {db_path}; run `agentbrowse build` first."
            )
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=60)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(db_path), timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        with contextlib.suppress(OSError):
            os.chmod(db_path, 0o600)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Item construction + writing
# --------------------------------------------------------------------------- #

def make_item(
    harness: str,
    session_id: str,
    line_no: int,
    relpath: str,
    kind: str,
    content: str,
    *,
    ts: Optional[str] = None,
    role: Optional[str] = None,
    call_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    display_title: Optional[str] = None,
    exit_code: Optional[int] = None,
    is_error: bool = False,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    text = content if isinstance(content, str) else flatten_text(content)
    return {
        "harness": harness,
        "session_id": session_id,
        "seq": line_no,
        "line_no": line_no,
        "source_relpath": relpath,
        "ts": clamp_ts(ts),
        "role": role,
        "kind": kind,
        "call_id": call_id,
        "tool_name": tool_name,
        "display_title": display_title,
        "content": text,
        "exit_code": exit_code,
        "is_error": 1 if is_error else 0,
        "meta": meta or {},
    }


def item_dedup_key(text: str, line_no: int, relpath: str, kind: str) -> str:
    if text:
        return sha256_text(text)
    return sha256_text(f"empty::{kind}::{relpath}::{line_no}")


def write_file_batch(
    conn: sqlite3.Connection,
    harness: str,
    session: Dict[str, Any],
    items: List[Dict[str, Any]],
    relpath: str,
) -> int:
    """Insert one file's items + upsert its session row. Returns items inserted."""
    inserted = 0
    session_id = session["session_id"]
    fts_rows: List[Tuple[Any, ...]] = []
    for it in items:
        text = it["content"] or ""
        sha = item_dedup_key(text, it["line_no"], it["source_relpath"], it["kind"])
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO items(
              harness, session_id, seq, line_no, source_relpath, ts, role, kind,
              call_id, tool_name, display_title, content, content_sha256,
              exit_code, is_error, meta_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                it["harness"], it["session_id"], it["seq"], it["line_no"],
                it["source_relpath"], it["ts"], it["role"], it["kind"], it["call_id"],
                it["tool_name"], it["display_title"], it["content"], sha,
                it["exit_code"], it["is_error"],
                json.dumps(it["meta"], ensure_ascii=False, separators=(",", ":")) if it["meta"] else None,
            ),
        )
        if cur.rowcount:
            inserted += 1
            item_id = cur.lastrowid
            fts_text = text[:FTS_TEXT_CAP]
            if fts_text.strip():
                fts_rows.append((fts_text, harness, session_id, it["kind"], it["role"], item_id))
    if fts_rows:
        conn.executemany("INSERT INTO items_fts VALUES(?,?,?,?,?,?)", fts_rows)

    _upsert_session(conn, harness, session, relpath)
    return inserted


def _upsert_session(conn: sqlite3.Connection, harness: str, session: Dict[str, Any], relpath: str) -> None:
    session_id = session["session_id"]
    counts = conn.execute(
        """
        SELECT COUNT(*) AS n,
               MIN(seq) AS min_seq, MAX(seq) AS max_seq,
               SUM(CASE WHEN kind='reasoning_encrypted' THEN 1 ELSE 0 END) AS enc,
               SUM(CASE WHEN kind IN ('thinking','reasoning_summary','agent_reasoning','reasoning_encrypted') THEN 1 ELSE 0 END) AS reasoning
        FROM items WHERE harness=? AND session_id=?
        """,
        (harness, session_id),
    ).fetchone()
    tsrow = conn.execute(
        """
        SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM items WHERE harness=? AND session_id=? AND ts IS NOT NULL
        """,
        (harness, session_id),
    ).fetchone()
    project = session.get("project")
    if not project and session.get("cwd"):
        project = pathlib.Path(str(session["cwd"])).name or str(session["cwd"])
    conn.execute(
        """
        INSERT INTO sessions(session_id, harness, kind, project, cwd, git_branch,
            first_ts, last_ts, model, entrypoint, cli_version, parent_session_id,
            title, n_items, has_encrypted_reasoning, max_reasoning_tokens, indexed_utc)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(harness, session_id) DO UPDATE SET
            kind=COALESCE(excluded.kind, kind),
            project=COALESCE(excluded.project, project),
            cwd=COALESCE(excluded.cwd, cwd),
            git_branch=COALESCE(excluded.git_branch, git_branch),
            first_ts=CASE WHEN sessions.first_ts IS NULL OR excluded.first_ts IS NULL
                          THEN COALESCE(sessions.first_ts, excluded.first_ts)
                          ELSE MIN(sessions.first_ts, excluded.first_ts) END,
            last_ts=CASE WHEN sessions.last_ts IS NULL OR excluded.last_ts IS NULL
                         THEN COALESCE(sessions.last_ts, excluded.last_ts)
                         ELSE MAX(sessions.last_ts, excluded.last_ts) END,
            model=COALESCE(excluded.model, model),
            entrypoint=COALESCE(excluded.entrypoint, entrypoint),
            cli_version=COALESCE(excluded.cli_version, cli_version),
            parent_session_id=COALESCE(excluded.parent_session_id, parent_session_id),
            title=COALESCE(excluded.title, title),
            n_items=excluded.n_items,
            has_encrypted_reasoning=excluded.has_encrypted_reasoning,
            max_reasoning_tokens=CASE WHEN sessions.max_reasoning_tokens IS NULL
                                      THEN excluded.max_reasoning_tokens
                                      ELSE MAX(sessions.max_reasoning_tokens, excluded.max_reasoning_tokens) END,
            indexed_utc=excluded.indexed_utc
        """,
        (
            session_id, harness, session.get("kind"), project, session.get("cwd"),
            session.get("git_branch"), tsrow["first_ts"], tsrow["last_ts"],
            session.get("model"), session.get("entrypoint"), session.get("cli_version"),
            session.get("parent_session_id"), session.get("title"),
            counts["n"], 1 if (counts["enc"] or 0) else 0,
            session.get("max_reasoning_tokens"), utc_now(),
        ),
    )


# --------------------------------------------------------------------------- #
# Claude extraction (whole-file; files are modest and we need the uuid map)
# --------------------------------------------------------------------------- #

def claude_kind_from_relpath(relpath: str) -> str:
    lower = relpath.replace("\\", "/").lower()
    if "/subagents/workflows/" in lower or "/workflows/" in lower:
        return "workflow"
    if "/subagents/" in lower:
        return "subagent"
    return "main"


def _claude_parent_prompt(by_uuid: Dict[str, Dict[str, Any]], start_uuid: Optional[str]) -> Tuple[Optional[str], str]:
    """Walk the parentUuid chain to the nearest real user prompt; return (uuid, snippet)."""
    seen: set = set()
    cur = start_uuid
    while cur and cur in by_uuid and cur not in seen:
        seen.add(cur)
        rec = by_uuid[cur]
        if rec.get("type") == "user" and not rec.get("isCompactSummary"):
            message = rec.get("message") or {}
            content = message.get("content")
            text = flatten_text(content)
            return cur, compact(text, 500)
        cur = rec.get("parentUuid")
    return None, ""


def extract_claude(path: pathlib.Path, relpath: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    session: Dict[str, Any] = {"kind": claude_kind_from_relpath(relpath)}
    records: List[Tuple[int, Dict[str, Any]]] = []
    by_uuid: Dict[str, Dict[str, Any]] = {}
    title: Optional[str] = None

    with path.open("rb") as fh:
        for line_no, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            records.append((line_no, obj))
            uid = obj.get("uuid")
            if isinstance(uid, str):
                by_uuid[uid] = obj
            for key in ("sessionId", "cwd", "gitBranch", "version", "entrypoint"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip() and not session.get(key):
                    session[key] = val
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                title = obj["aiTitle"]
            elif t == "custom-title" and isinstance(obj.get("customTitle"), str):
                title = obj["customTitle"] or title
            message = obj.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                session["model"] = message["model"]
            if isinstance(obj.get("cwd"), str):
                session["cwd"] = obj["cwd"]
            if isinstance(obj.get("gitBranch"), str):
                session["git_branch"] = obj["gitBranch"]

    if title:
        session["title"] = title
    session.setdefault("cwd", None)
    session.setdefault("git_branch", None)

    session_id = path_session_id("claude", relpath, records[0][1]) if records else path.stem
    session["session_id"] = session_id

    items: List[Dict[str, Any]] = []
    for line_no, obj in records:
        t = obj.get("type")
        ts = obj.get("timestamp")
        uuid = obj.get("uuid")
        if t == "assistant":
            message = obj.get("message") or {}
            model = message.get("model") if isinstance(message.get("model"), str) else None
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                bt = blk.get("type")
                if bt == "thinking":
                    puuid, psnip = _claude_parent_prompt(by_uuid, obj.get("parentUuid"))
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "thinking",
                        blk.get("thinking") or "", ts=ts, role="assistant",
                        meta={"model": model, "signature_len": len(blk.get("signature") or ""),
                              "parent_user_uuid": puuid, "parent_prompt_snippet": psnip},
                    ))
                elif bt == "text":
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "text",
                        blk.get("text") or "", ts=ts, role="assistant", meta={"model": model}))
                elif bt == "tool_use":
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "tool_use",
                        json.dumps(blk.get("input") or {}, ensure_ascii=False, sort_keys=True),
                        ts=ts, role="assistant", call_id=blk.get("id"),
                        tool_name=blk.get("name"),
                        display_title=claude_tool_title(blk.get("name"), blk.get("input")),
                        meta={"model": model}))
                elif bt == "redacted_thinking":
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, REASONING_OPAQUE_KIND, "",
                        ts=ts, role="assistant",
                        meta={"opaque": "redacted_thinking", "data_len": len(blk.get("data") or "")}))
                elif bt == "server_tool_use":
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "server_tool_use",
                        json.dumps(blk, ensure_ascii=False, sort_keys=True)[:4000],
                        ts=ts, role="assistant", tool_name=blk.get("name")))
        elif t == "user":
            message = obj.get("message") or {}
            content = message.get("content")
            is_compact = bool(obj.get("isCompactSummary"))
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        items.append(make_item(
                            "claude", session_id, line_no, relpath, "tool_result",
                            flatten_text(blk.get("content")), ts=ts, role="tool",
                            call_id=blk.get("tool_use_id"),
                            is_error=bool(blk.get("is_error")),
                            meta={"source_tool_assistant_uuid": obj.get("sourceToolAssistantUUID")}))
                text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if text_blocks and not is_compact:
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "message",
                        "\n".join(b.get("text", "") for b in text_blocks),
                        ts=ts, role="user"))
            elif isinstance(content, str):
                if is_compact:
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "compact_summary", content,
                        ts=ts, role="system",
                        meta={"logical_parent_uuid": obj.get("logicalParentUuid")}))
                else:
                    items.append(make_item(
                        "claude", session_id, line_no, relpath, "message", content, ts=ts, role="user"))
        elif t == "system":
            subtype = obj.get("subtype")
            body = obj.get("content")
            text = body if isinstance(body, str) else ""
            if subtype == "compact_boundary":
                items.append(make_item(
                    "claude", session_id, line_no, relpath, "compact_summary",
                    text or "Conversation compacted", ts=ts, role="system",
                    meta={"subtype": subtype, "compact_metadata_present": "compactMetadata" in obj}))
            elif text.strip():
                items.append(make_item(
                    "claude", session_id, line_no, relpath, "system", text, ts=ts, role="system",
                    meta={"subtype": subtype}))
        elif t == "attachment":
            att = obj.get("attachment") or {}
            inner = att.get("type")
            raw_text = att.get("text")
            if not isinstance(raw_text, str):
                raw_text = flatten_text(att.get("content") if "content" in att else att)
            items.append(make_item(
                "claude", session_id, line_no, relpath, "attachment",
                compact(raw_text, 1500), ts=ts, role="system",
                display_title=inner if isinstance(inner, str) else None,
                meta={"attachment_type": inner}))

    return session, items


# --------------------------------------------------------------------------- #
# Codex extraction (streaming; supports flat + envelope formats)
# --------------------------------------------------------------------------- #

class CodexState:
    __slots__ = ("session_id", "cwd", "git_branch", "originator", "cli_version",
                 "parent_session_id", "model", "first_ts", "last_ts",
                 "has_encrypted", "max_reasoning_tokens")

    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.cwd: Optional[str] = None
        self.git_branch: Optional[str] = None
        self.originator: Optional[str] = None
        self.cli_version: Optional[str] = None
        self.parent_session_id: Optional[str] = None
        self.model: Optional[str] = None
        self.first_ts: Optional[str] = None
        self.last_ts: Optional[str] = None
        self.has_encrypted = False
        self.max_reasoning_tokens: Optional[int] = None

    def session_dict(self, relpath: str) -> Dict[str, Any]:
        sid = self.session_id or _codex_session_from_relpath(relpath)
        project = pathlib.Path(self.cwd).name if self.cwd else None
        return {
            "session_id": sid, "kind": "main", "project": project, "cwd": self.cwd,
            "git_branch": self.git_branch, "model": self.model,
            "entrypoint": self.originator, "cli_version": self.cli_version,
            "parent_session_id": self.parent_session_id,
            "has_encrypted_reasoning": self.has_encrypted,
            "max_reasoning_tokens": self.max_reasoning_tokens,
        }


def _codex_session_from_relpath(relpath: str) -> str:
    m = UUID_FULL_RE.search(relpath)
    if m:
        return m.group(0).lower()
    return pathlib.Path(relpath).stem


def _codex_message_content(payload: Dict[str, Any]) -> str:
    return flatten_text(payload.get("content"))


def extract_codex(
    path: pathlib.Path, relpath: str, start_offset: int = 0, start_line_no: int = 0
) -> Tuple[CodexState, List[Dict[str, Any]], int, int]:
    """Stream a Codex rollout file.

    Returns (state, items, safe_byte_offset, absolute_line_no). Only the safe
    offset and line counter advance past fully newline-terminated lines, so a
    half-written trailing line is re-read next run. Absolute line numbers
    continue from ``start_line_no`` so resumed items sort correctly behind
    previously-indexed items.
    """
    state = CodexState()
    items: List[Dict[str, Any]] = []
    safe_offset = start_offset
    line_no = start_line_no
    with path.open("rb") as fh:
        fh.seek(start_offset)
        while True:
            line_start = fh.tell()
            raw = fh.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if raw.endswith((b"\n", b"\r")):
                line_no += 1
                safe_offset = fh.tell()
                if text.strip():
                    with contextlib.suppress(Exception):
                        _codex_process(json.loads(text), state, items, relpath, line_no)
            else:
                # No trailing newline: commit only if this is genuine EOF.
                peek = fh.read(1)
                if not peek:
                    line_no += 1
                    safe_offset = fh.tell()
                    if text.strip():
                        with contextlib.suppress(Exception):
                            _codex_process(json.loads(text), state, items, relpath, line_no)
                else:
                    fh.seek(line_start)  # rewind; re-read next run
                break
    if not state.session_id:
        state.session_id = _codex_session_from_relpath(relpath)
    return state, items, safe_offset, line_no


def _codex_process(obj: Dict[str, Any], state: CodexState, items: List[Dict[str, Any]], relpath: str, line_no: int) -> None:
    top_type = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
    ts = obj.get("timestamp")
    if isinstance(ts, str) and ts.strip():
        state.first_ts = state.first_ts or ts
        state.last_ts = ts

    # Envelope-only types (no inner payload.type that is a content item).
    if top_type == "session_meta":
        p = payload or {}
        if isinstance(p.get("id"), str):
            state.session_id = p["id"].lower()
        if isinstance(p.get("cwd"), str):
            state.cwd = p["cwd"]
        git = p.get("git")
        if isinstance(git, dict) and isinstance(git.get("branch"), str):
            state.git_branch = git["branch"]
        if isinstance(p.get("originator"), str):
            state.originator = p["originator"]
        if isinstance(p.get("cli_version"), str):
            state.cli_version = p["cli_version"]
        state.parent_session_id = p.get("parent_thread_id") or p.get("forked_from_id") or state.parent_session_id
        return
    if top_type == "turn_context":
        p = payload or {}
        if isinstance(p.get("model"), str):
            state.model = p["model"]
        if isinstance(p.get("cwd"), str):
            state.cwd = p["cwd"]
        return
    if top_type in ("world_state", "inter_agent_communication_metadata"):
        return
    if top_type == "compacted":
        p = payload or {}
        msg = p.get("message")
        items.append(make_item(
            "codex", state.session_id or _codex_session_from_relpath(relpath), line_no, relpath,
            "compact_summary", msg if isinstance(msg, str) else flatten_text(p.get("replacement_history")),
            ts=ts, role="system", meta={"envelope": "compacted"}))
        return

    # Flat-format header line ({id, timestamp, instructions, git}, no type).
    if top_type is None and isinstance(obj.get("id"), str):
        state.session_id = obj["id"].lower()
        if isinstance(obj.get("timestamp"), str):
            state.first_ts = obj["timestamp"]
            state.last_ts = obj["timestamp"]
        git = obj.get("git")
        if isinstance(git, dict) and isinstance(git.get("branch"), str):
            state.git_branch = git["branch"]
        return
    if obj.get("record_type") == "state":
        return

    inner = payload.get("type") if payload else top_type
    data = payload if payload else obj
    sid = state.session_id or _codex_session_from_relpath(relpath)

    if top_type == "event_msg" or (payload is None and top_type == "event_msg"):
        _codex_event(payload or {}, state, items, sid, relpath, ts, line_no)
        return

    if top_type == "response_item" or (payload is None and top_type in FLAT_CODEX_TYPES):
        _codex_response_item(inner, data, state, items, sid, relpath, ts, line_no)
        return

    # Unknown envelope — keep a compact breadcrumb so no token is silently lost.
    items.append(make_item(
        "codex", sid, line_no, relpath, str(inner or top_type or "unknown"),
        json.dumps(_scrub_b64(data), ensure_ascii=False)[:3000],
        ts=ts, role="system", meta={"envelope": top_type}))


def _codex_response_item(inner: Optional[str], data: Dict[str, Any], state: CodexState,
                         items: List[Dict[str, Any]], sid: str, relpath: str, ts: Optional[str],
                         line_no: int) -> None:
    if inner == "message":
        role = data.get("role")
        text = _codex_message_content(data)
        items.append(make_item("codex", sid, line_no, relpath, "message", text, ts=ts, role=role,
                               meta={"model": state.model}))
    elif inner == "reasoning":
        summary = data.get("summary") or []
        sumtext = "\n".join(
            b.get("text", "") for b in summary
            if isinstance(b, dict) and b.get("type") == "summary_text"
        )
        enc = data.get("encrypted_content")
        enc_len = len(enc) if isinstance(enc, str) else 0
        if enc_len:
            state.has_encrypted = True
        meta = {"encrypted_len": enc_len, "has_encrypted": bool(enc_len), "id": data.get("id")}
        if sumtext.strip():
            items.append(make_item("codex", sid, line_no, relpath, "reasoning_summary", sumtext,
                                   ts=ts, role="reasoning", meta=meta))
        else:
            items.append(make_item("codex", sid, line_no, relpath, REASONING_OPAQUE_KIND, "",
                                   ts=ts, role="reasoning", meta=meta))
    elif inner == "function_call":
        args = data.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, sort_keys=True) if args is not None else ""
        items.append(make_item("codex", sid, line_no, relpath, "function_call", args, ts=ts,
                               role="tool", call_id=data.get("call_id"), tool_name=data.get("name"),
                               display_title=codex_call_title(data.get("name"), args)))
    elif inner == "function_call_output":
        items.append(make_item("codex", sid, line_no, relpath, "function_call_output",
                               data.get("output") or "", ts=ts, role="tool", call_id=data.get("call_id")))
    elif inner == "custom_tool_call":
        inp = data.get("input")
        if not isinstance(inp, str):
            inp = json.dumps(inp, ensure_ascii=False, sort_keys=True) if inp is not None else ""
        items.append(make_item("codex", sid, line_no, relpath, "custom_tool_call", inp, ts=ts,
                               role="tool", call_id=data.get("call_id"), tool_name=data.get("name"),
                               display_title=codex_call_title(data.get("name"), inp)))
    elif inner == "custom_tool_call_output":
        out = data.get("output") or ""
        exit_code: Optional[int] = None
        with contextlib.suppress(Exception):
            parsed = json.loads(out) if isinstance(out, str) else out
            if isinstance(parsed, dict):
                md = parsed.get("metadata") or {}
                if isinstance(md.get("exit_code"), int):
                    exit_code = md["exit_code"]
        items.append(make_item("codex", sid, line_no, relpath, "custom_tool_call_output", out,
                               ts=ts, role="tool", call_id=data.get("call_id"), exit_code=exit_code,
                               is_error=(exit_code not in (None, 0))))
    elif inner == "web_search_call":
        action = data.get("action") or {}
        queries = action.get("queries") if isinstance(action, dict) else None
        items.append(make_item("codex", sid, line_no, relpath, "web_search",
                               json.dumps(data, ensure_ascii=False)[:4000], ts=ts, role="tool",
                               call_id=data.get("call_id"),
                               display_title="web_search: " + (" ".join(queries)[:100] if queries else "")))
    elif inner in ("tool_search_call", "agent_message"):
        items.append(make_item("codex", sid, line_no, relpath, inner,
                               json.dumps(_scrub_b64(data), ensure_ascii=False)[:4000], ts=ts,
                               role="system", call_id=data.get("call_id"), meta={"_inner": inner}))
    else:
        items.append(make_item("codex", sid, line_no, relpath, str(inner or "response_item_unknown"),
                               json.dumps(_scrub_b64(data), ensure_ascii=False)[:3000], ts=ts, role="system"))


def _codex_event(payload: Dict[str, Any], state: CodexState, items: List[Dict[str, Any]],
                 sid: str, relpath: str, ts: Optional[str], line_no: int) -> None:
    et = payload.get("type")
    if et == "agent_reasoning":
        items.append(make_item("codex", sid, line_no, relpath, "agent_reasoning",
                               payload.get("text") or "", ts=ts, role="reasoning"))
    elif et == "agent_message":
        items.append(make_item("codex", sid, line_no, relpath, "text",
                               payload.get("message") or "", ts=ts, role="assistant"))
    elif et == "user_message":
        items.append(make_item("codex", sid, line_no, relpath, "message",
                               payload.get("message") or "", ts=ts, role="user"))
    elif et == "exec_command_end":
        out = payload.get("aggregated_output") or payload.get("stdout") or ""
        exit_code = payload.get("exit_code")
        if not isinstance(exit_code, int):
            exit_code = None
        stderr = payload.get("stderr") or ""
        duration = payload.get("duration") or {}
        dur_sec = duration.get("secs") if isinstance(duration, dict) else None
        items.append(make_item("codex", sid, line_no, relpath, "exec_end", out, ts=ts, role="tool",
                               call_id=payload.get("call_id"), tool_name="exec_command",
                               exit_code=exit_code, is_error=(exit_code not in (None, 0)),
                               display_title=cmd_title(payload.get("command")),
                               meta={"command": payload.get("command"), "cwd": payload.get("cwd"),
                                     "stderr": stderr[:4000], "duration_sec": dur_sec,
                                     "source": "exec_command_end"}))
    elif et == "patch_apply_end":
        success = payload.get("success")
        stdout = payload.get("stdout") or ""
        changes = payload.get("changes") or {}
        meta = {"changes_paths": list(changes.keys())[:50] if isinstance(changes, dict) else [],
                "stderr": (payload.get("stderr") or "")[:4000], "success": success,
                "source": "patch_apply_end"}
        items.append(make_item("codex", sid, line_no, relpath, "patch_end", stdout, ts=ts, role="tool",
                               call_id=payload.get("call_id"), tool_name="apply_patch",
                               exit_code=(0 if success else 1), is_error=not success,
                               display_title="apply_patch", meta=meta))
    elif et == "token_count":
        info = payload.get("info") or {}
        last = info.get("last_token_usage") or {}
        rot = last.get("reasoning_output_tokens")
        if isinstance(rot, int):
            state.max_reasoning_tokens = max(state.max_reasoning_tokens or 0, rot)
    elif et in ("task_started", "task_complete"):
        items.append(make_item("codex", sid, line_no, relpath, "turn_boundary", et, ts=ts,
                               role="system", meta={"turn_id": payload.get("turn_id"),
                                                    "duration_ms": payload.get("duration_ms")}))
    # Other event_msg types are UI/streaming noise already represented by the
    # durable response_items above; skip to avoid duplication.


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def _scan_files(harness: str) -> List[Tuple[pathlib.Path, str]]:
    root = HARNESS_ROOTS[harness]
    out: List[Tuple[pathlib.Path, str]] = []
    for sub in SCAN_SUBDIRS[harness]:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.jsonl")):
            if p.is_file():
                out.append((p, p.relative_to(root).as_posix()))
    return out


def _index_one(conn: sqlite3.Connection, harness: str, path: pathlib.Path, relpath: str) -> Tuple[str, int]:
    info = path.stat()
    prefix = prefix_sha256(path)
    prior = conn.execute(
        "SELECT * FROM scan_state WHERE harness=? AND relpath=?", (harness, relpath)
    ).fetchone()

    # Cheap skip: unchanged on disk.
    if prior and prior["size_bytes"] == info.st_size and prior["mtime_ns"] == info.st_mtime_ns:
        return "skipped", 0

    start_offset = 0
    start_line_no = 0
    last_model: Optional[str] = None
    last_cwd: Optional[str] = None
    if (
        prior
        and prior["prefix_sha256"] == prefix
        and info.st_size >= prior["byte_offset"]
    ):
        start_offset = prior["byte_offset"]
        start_line_no = prior["line_no"] or 0
        with contextlib.suppress(Exception):
            pm = json.loads(prior["meta_json"] or "{}")
            last_model = pm.get("last_model")
            last_cwd = pm.get("last_cwd")

    if harness == "claude":
        session, items = extract_claude(path, relpath)
        new_offset = info.st_size
        new_line_no = len(items)
    else:
        state, items, new_offset, new_line_no = extract_codex(path, relpath, start_offset, start_line_no)
        if last_model and not state.model:
            state.model = last_model
        if last_cwd and not state.cwd:
            state.cwd = last_cwd
        session = state.session_dict(relpath)

    inserted = write_file_batch(conn, harness, session, items, relpath)

    conn.execute(
        """
        INSERT INTO scan_state(harness, relpath, size_bytes, mtime_ns, byte_offset,
            line_no, prefix_sha256, meta_json, indexed_utc)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(harness, relpath) DO UPDATE SET
            size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,
            byte_offset=excluded.byte_offset, line_no=excluded.line_no,
            prefix_sha256=excluded.prefix_sha256, meta_json=excluded.meta_json,
            indexed_utc=excluded.indexed_utc
        """,
        (harness, relpath, info.st_size, info.st_mtime_ns, new_offset, new_line_no,
         prefix, json.dumps({"last_model": session.get("model"), "last_cwd": session.get("cwd")}),
         utc_now()),
    )
    return ("updated" if start_offset else "indexed"), inserted


def cmd_build(args: argparse.Namespace) -> int:
    db = pathlib.Path(args.db)
    conn = connect(db, readonly=False)
    harnesses = [args.harness] if args.harness != "all" else ["claude", "codex"]
    try:
        for harness in harnesses:
            files = _scan_files(harness)
            if args.limit:
                files = files[: args.limit]
            counts = {"indexed": 0, "updated": 0, "skipped": 0, "failed": 0}
            items_inserted = 0
            for idx, (path, relpath) in enumerate(files, 1):
                try:
                    status, inserted = _index_one(conn, harness, path, relpath)
                    counts[status] = counts.get(status, 0) + 1
                    items_inserted += inserted
                except Exception:
                    counts["failed"] += 1
                    if args.verbose:
                        sys.stderr.write(f"  failed: {relpath}\n{traceback.format_exc()}")
                if idx % 200 == 0:
                    conn.commit()
                    sys.stderr.write(f"  {harness}: {idx}/{len(files)} files...\n")
            conn.commit()
            sys.stderr.write(
                f"{harness}: {len(files)} files | "
                f"indexed={counts['indexed']} updated={counts['updated']} "
                f"skipped={counts['skipped']} failed={counts['failed']} | "
                f"+{items_inserted} items\n"
            )
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect(pathlib.Path(args.db), readonly=True)
    try:
        db_size = pathlib.Path(args.db).stat().st_size
        per = conn.execute(
            """
            SELECT harness,
                   COUNT(DISTINCT session_id) AS sessions,
                   COUNT(*) AS items,
                   SUM(CASE WHEN kind='thinking' THEN 1 ELSE 0 END) AS thinking,
                   SUM(CASE WHEN kind='reasoning_summary' THEN 1 ELSE 0 END) AS reasoning_summary,
                   SUM(CASE WHEN kind='agent_reasoning' THEN 1 ELSE 0 END) AS agent_reasoning,
                   SUM(CASE WHEN kind='reasoning_encrypted' THEN 1 ELSE 0 END) AS reasoning_encrypted,
                   SUM(CASE WHEN kind IN ('tool_use','function_call','custom_tool_call') THEN 1 ELSE 0 END) AS tool_calls,
                   SUM(CASE WHEN kind IN ('tool_result','function_call_output','custom_tool_call_output','exec_end','patch_end') THEN 1 ELSE 0 END) AS tool_results
            FROM items GROUP BY harness
            """
        ).fetchall()
        enc_bytes = conn.execute(
            "SELECT SUM(CAST(json_extract(meta_json,'$.encrypted_len') AS INTEGER)) FROM items WHERE kind='reasoning_encrypted'"
        ).fetchone()[0] or 0
        print(f"agent_browse db: {args.db}  ({_human_bytes(db_size)})")
        print()
        hdr = f"{'harness':<8} {'sessions':>9} {'items':>9} {'thinking':>9} {'rsn_sum':>8} {'ag_rsn':>8} {'rsn_enc':>8} {'calls':>7} {'results':>8}"
        print(hdr)
        print("-" * len(hdr))
        for r in per:
            print(f"{r['harness']:<8} {r['sessions']:>9} {r['items']:>9} {r['thinking']:>9} "
                  f"{r['reasoning_summary']:>8} {r['agent_reasoning']:>8} {r['reasoning_encrypted']:>8} "
                  f"{r['tool_calls']:>7} {r['tool_results']:>8}")
        print()
        print(f"Codex reasoning marked encrypted (content NOT recoverable): "
              f"{(r and r['reasoning_encrypted']) or 0} items, ~{_human_bytes(enc_bytes)} opaque bytes indexed as length-only.")
        print("Claude thinking blocks are full plaintext chain-of-thought.")
    finally:
        conn.close()
    return 0


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore
    return f"{n} B"


# --------------------------------------------------------------------------- #
# Search / show
# --------------------------------------------------------------------------- #

def cmd_search(args: argparse.Namespace) -> int:
    conn = connect(pathlib.Path(args.db), readonly=True)
    try:
        variants = fts_query_variants(args.query)
        kinds = parse_args_list(args.kind)
        harnesses = [args.harness] if args.harness in ("claude", "codex") else ["claude", "codex"]
        results: List[Dict[str, Any]] = []
        for tier, match in enumerate(variants):
            for harness in harnesses:
                sql = (
                    "SELECT items_fts.item_id AS item_id, i.kind AS kind, i.role AS role, "
                    "i.session_id AS session_id, i.source_relpath AS source_relpath, "
                    "i.line_no AS line_no, "
                    "snippet(items_fts, 0, '<<', '>>', '…', 18) AS excerpt, "
                    "bm25(items_fts) AS bm25 "
                    "FROM items_fts JOIN items i ON i.item_id = items_fts.item_id "
                    "WHERE items_fts MATCH ? AND i.harness=?"
                )
                params: List[Any] = [match, harness]
                if kinds:
                    sql += f" AND i.kind IN ({','.join('?' for _ in kinds)})"
                    params.extend(kinds)
                if args.session:
                    sql += " AND i.session_id=?"
                    params.append(args.session)
                sql += " ORDER BY bm25(items_fts) LIMIT ?"
                params.append(args.limit * 3)
                with contextlib.suppress(sqlite3.OperationalError):
                    for row in conn.execute(sql, params):
                        results.append({
                            "hit_id": f"i:{harness}:{row['item_id']}",
                            "match_tier": tier,
                            "harness": harness,
                            "source": "incremental",
                            "session_id": row["session_id"],
                            "source_relpath": row["source_relpath"],
                            "line_no": row["line_no"],
                            "kind": row["kind"],
                            "role": row["role"],
                            "excerpt": row["excerpt"],
                        })
        ranked = balanced_search_results(results, args.limit)
        if args.json:
            print(json.dumps(ranked, ensure_ascii=False, indent=2))
            return 0
        if not ranked:
            print("no matches.")
            return 1
        for i, item in enumerate(ranked, 1):
            tier_label = "all-signal" if item.get("match_tier", 1) == 0 else "broad"
            coord = f"{item['harness']}:{item['session_id'][:8]}:{item['line_no']}"
            print(f"{i}. [{tier_label} {item['harness']} {item['kind']}] {item['hit_id']}  ({coord})")
            print(f"   …{item['excerpt']}…")
        print(f"\nshow full text: agentbrowse show <hit_id>")
    finally:
        conn.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    m = HIT_RE.fullmatch(args.hit_id)
    if not m:
        # Allow a bare session id to be passed through to `session`.
        if UUID_FULL_RE.fullmatch(args.hit_id):
            return cmd_session(argparse.Namespace(**{**vars(args), "session_id": args.hit_id, "kind": None}))
        sys.stderr.write(f"unrecognized hit id: {args.hit_id} (expected i:claude|codex:NNN)\n")
        return 2
    harness, item_id = m.group(1), int(m.group(2))
    conn = connect(pathlib.Path(args.db), readonly=True)
    try:
        row = conn.execute("SELECT * FROM items WHERE harness=? AND item_id=?", (harness, item_id)).fetchone()
        if not row:
            sys.stderr.write("no such item.\n")
            return 1
        content = row["content"] or ""
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        prefix = (f"ITEM {harness}:{row['session_id']} "
                  f"kind={row['kind']} role={row['role']} line={row['line_no']}")
        if row["tool_name"]:
            prefix += f" tool={row['tool_name']}"
        if row["exit_code"] is not None:
            prefix += f" exit={row['exit_code']}"
        if row["display_title"]:
            prefix += f"\ntitle: {row['display_title']}"
        prefix += f"\nbudget={args.max_chars}"
        body = content
        suffix = ""
        if len(prefix) + len(body) > args.max_chars:
            avail = max(0, args.max_chars - len(prefix) - 40)
            body = body[:avail]
            suffix = f"\n[TRUNCATED omitted_chars={len(content) - len(body)}]"
        print(prefix)
        print(body + suffix)
        if meta:
            print(f"\n[meta] {json.dumps(meta, ensure_ascii=False)}")
        print(f"\n[source] {row['source_relpath']}")
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------- #
# Sessions / session replay / export
# --------------------------------------------------------------------------- #

def cmd_sessions(args: argparse.Namespace) -> int:
    conn = connect(pathlib.Path(args.db), readonly=True)
    try:
        where = []
        params: List[Any] = []
        if args.harness in ("claude", "codex"):
            where.append("harness=?")
            params.append(args.harness)
        if args.project:
            where.append("(LOWER(project) LIKE ? OR LOWER(cwd) LIKE ?)")
            params += [f"%{args.project.lower()}%", f"%{args.project.lower()}%"]
        if args.since:
            where.append("last_ts >= ?")
            params.append(args.since)
        if args.encrypted:
            where.append("has_encrypted_reasoning=1")
        sql = "SELECT * FROM sessions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_ts DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(sql, params).fetchall()
        if args.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no sessions indexed.")
            return 1
        for r in rows:
            title = (r["title"] or "").strip()
            print(f"[{r['harness']:<5}] {r['session_id']}  items={r['n_items']}")
            print(f"        project={r['project'] or r['cwd'] or '?'}  model={r['model'] or '?'}  "
                  f"{(r['first_ts'] or '?')[:10]}..{(r['last_ts'] or '?')[:10]}")
            if title:
                print(f"        title: {title[:140]}")
        print(f"\n{len(rows)} session(s). replay: agentbrowse session <session_id>")
    finally:
        conn.close()
    return 0


def _load_session_items(conn: sqlite3.Connection, harness: str, session_id: str,
                        kinds: Optional[List[str]]) -> List[sqlite3.Row]:
    sql = ("SELECT * FROM items WHERE harness=? AND session_id=? ")
    params: List[Any] = [harness, session_id]
    if kinds:
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params += kinds
    sql += " ORDER BY source_relpath, seq, item_id"
    return conn.execute(sql, params).fetchall()


def _resolve_session(conn: sqlite3.Connection, session_id: str) -> Optional[Tuple[str, str]]:
    row = conn.execute(
        "SELECT harness, session_id FROM sessions WHERE session_id=? ORDER BY harness LIMIT 1",
        (session_id.lower(),),
    ).fetchone()
    if row:
        return row["harness"], row["session_id"]
    # Fuzzy: prefix match.
    row = conn.execute(
        "SELECT harness, session_id FROM sessions WHERE session_id LIKE ? ORDER BY last_ts DESC LIMIT 1",
        (session_id.lower() + "%",),
    ).fetchone()
    return (row["harness"], row["session_id"]) if row else None


def _render_item(row: sqlite3.Row, cap: int) -> str:
    kind = row["kind"]
    role = row["role"] or ""
    head = f"[{kind}/{role}]"
    if row["display_title"]:
        head += f" {row['display_title']}"
    if row["exit_code"] is not None:
        head += f"  (exit {row['exit_code']})"
    content = row["content"] or ""
    suffix = ""
    if cap and len(content) > cap:
        content = content[:cap]
        suffix = f" …[+{len(row['content']) - cap} chars truncated]"
    sep = "\n    "
    body = content.replace("\n", sep) if content else ""
    if kind in REASONING_KINDS:
        return f"  ◇ {head}\n    \033[2m{body}{suffix}\033[0m"
    if kind == REASONING_OPAQUE_KIND:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        return f"  ◇ {head}  \033[2m[encrypted reasoning — not recoverable; {meta.get('encrypted_len', 0)} opaque bytes]\033[0m"
    if kind in TOOL_CALL_KINDS:
        return f"  ▶ {head}\n    \033[33m{body}{suffix}\033[0m"
    if kind in TOOL_RESULT_KINDS:
        return f"  ◀ {head}\n    \033[36m{body}{suffix}\033[0m"
    if role == "user":
        return f"\n\033[1mYOU ▸\033[0m {body}{suffix}"
    if role == "assistant":
        return f"\033[1mAGENT ▸\033[0m {body}{suffix}"
    if kind == "compact_summary":
        return f"  ⚑ [compact_summary] {body}{suffix}"
    return f"  · {head}\n    {body}{suffix}"


def cmd_session(args: argparse.Namespace) -> int:
    conn = connect(pathlib.Path(args.db), readonly=True)
    try:
        resolved = _resolve_session(conn, args.session_id)
        if not resolved:
            sys.stderr.write(f"no session matching {args.session_id!r}. try `agentbrowse sessions`.\n")
            return 1
        harness, session_id = resolved
        kinds = parse_args_list(args.kind)
        rows = _load_session_items(conn, harness, session_id, kinds)
        srow = conn.execute("SELECT * FROM sessions WHERE harness=? AND session_id=?", (harness, session_id)).fetchone()
        out_lines: List[str] = []
        out_lines.append(f"# session {harness}/{session_id}  ({len(rows)} items)")
        if srow:
            out_lines.append(f"# project={srow['project'] or srow['cwd']}  model={srow['model']}  "
                             f"{srow['first_ts']}..{srow['last_ts']}  encrypted_reasoning={bool(srow['has_encrypted_reasoning'])}")
            if srow["title"]:
                out_lines.append(f"# title: {srow['title']}")
        out_lines.append("")
        for row in rows:
            out_lines.append(_render_item(row, args.max_chars))
        text = "\n".join(out_lines)
        if args.out:
            pathlib.Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {args.out} ({len(text)} chars)")
        else:
            sys.stdout.write(text + "\n")
    finally:
        conn.close()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentbrowse", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"path to index DB (default: {DEFAULT_DB})")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("build", help="legacy benchmark: duplicate raw generated tokens into another DB")
    sp.add_argument("--harness", choices=["claude", "codex", "all"], default="all")
    sp.add_argument("--limit", type=int, default=0, help="only index N files per harness (testing)")
    sp.add_argument("--verbose", action="store_true", help="print per-file tracebacks on failure")
    sp.add_argument(
        "--i-understand-this-duplicates-data", action="store_true", required=True,
        help="acknowledge that this creates a rebuildable multi-gigabyte duplicate",
    )
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("stats", help="honest per-harness accounting of what was indexed")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("search", help="two-stage FTS search across reasoning + actions")
    sp.add_argument("query")
    sp.add_argument("--kind", help="comma-separated kinds, e.g. thinking,reasoning_summary,agent_reasoning")
    sp.add_argument("--harness", choices=["claude", "codex"])
    sp.add_argument("--session", help="restrict to a session id")
    sp.add_argument("--limit", type=int, default=8)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("show", help="show full text of one hit (i:harness:NNN)")
    sp.add_argument("hit_id")
    sp.add_argument("--max-chars", type=int, default=6000)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("sessions", help="list sessions")
    sp.add_argument("--harness", choices=["claude", "codex"])
    sp.add_argument("--project", help="substring match on project/cwd")
    sp.add_argument("--since", help="ISO date, e.g. 2026-08-01")
    sp.add_argument("--encrypted", action="store_true", help="only sessions with encrypted reasoning")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("session", help="replay one session in order (every item)")
    sp.add_argument("session_id")
    sp.add_argument("--kind", help="comma-separated kinds to include")
    sp.add_argument("--max-chars", type=int, default=3000, help="per-item content cap in CLI view")
    sp.add_argument("--out", help="write markdown to a file instead of stdout")
    sp.set_defaults(func=cmd_session)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
