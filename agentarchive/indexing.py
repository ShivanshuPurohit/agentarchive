"""Transcript extraction and incremental indexing with source provenance."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
from privacy_filter import workspace_is_excluded
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import common as utils, reports
from .common import COMMIT_RE, GITHUB_ISSUE_RE, GITHUB_PR_RE, UUID_RE


TOOL_CALLS_VERSION = 1
CODEX_TOOL_TYPES = {
    "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output",
}


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def after_cutoff(timestamp: Any, cutoff: str) -> bool:
    value = parse_timestamp(timestamp)
    boundary = parse_timestamp(cutoff)
    return bool(value and boundary and value > boundary)


def path_session_id(harness: str, relative: str, obj: Dict[str, Any]) -> str:
    for key in ("sessionId", "session_id", "sessionID"):
        value = obj.get(key)
        if isinstance(value, str) and UUID_RE.fullmatch(value):
            return value.lower()
    payload = obj.get("payload")
    if harness == "codex" and isinstance(payload, dict):
        value = payload.get("id")
        if isinstance(value, str) and UUID_RE.fullmatch(value):
            return value.lower()
    matches = UUID_RE.findall(relative)
    if harness == "claude" and matches:
        return matches[0].lower()
    if matches:
        return matches[-1].lower()
    return pathlib.Path(relative).stem


def text_from_content(value: Any) -> List[Tuple[str, Optional[str], str]]:
    """Return (kind, tool_name, text) without silently dropping unknown text."""
    found: List[Tuple[str, Optional[str], str]] = []
    if value is None:
        return found
    if isinstance(value, str):
        if value.strip():
            found.append(("text", None, value))
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(text_from_content(item))
        return found
    if not isinstance(value, dict):
        return found
    kind = str(value.get("type") or value.get("kind") or "object")
    tool_name = value.get("name") if isinstance(value.get("name"), str) else None
    emitted = False
    for key in ("text", "thinking", "summary", "output_text", "input_text", "message"):
        text = value.get(key)
        if isinstance(text, str) and text.strip():
            found.append((kind, tool_name, text))
            emitted = True
    for key in ("content", "output", "result", "toolUseResult", "tool_result"):
        nested = value.get(key)
        if nested is not None:
            for nested_kind, nested_name, text in text_from_content(nested):
                found.append((kind if kind != "object" else nested_kind, tool_name or nested_name, text))
                emitted = True
    arguments = value.get("arguments")
    if arguments is None and kind in {"tool_use", "function_call", "custom_tool_call"}:
        arguments = value.get("input")
    if arguments is not None:
        if isinstance(arguments, str):
            text = arguments
        else:
            text = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        if text.strip():
            found.append(("tool_call", tool_name, text))
            emitted = True
    if not emitted and kind in {"tool_result", "function_call_output", "custom_tool_call_output"}:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        found.append((kind, tool_name, text))
    return found


def extract_event(
    harness: str,
    relative: str,
    obj: Dict[str, Any],
) -> Tuple[str, Optional[str], Optional[str], Optional[str], List[Tuple[str, Optional[str], str, Optional[str]]], Dict[str, Any]]:
    timestamp = obj.get("timestamp")
    session_id = path_session_id(harness, relative, obj)
    parent_id = None
    cwd = None
    model = None
    blocks: List[Tuple[str, Optional[str], str, Optional[str]]] = []
    metadata: Dict[str, Any] = {}
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    message = obj.get("message") if isinstance(obj.get("message"), dict) else None

    if harness == "codex":
        # Older rollouts store tool records directly; newer ones wrap a payload.
        if not payload and obj.get("type") in CODEX_TOOL_TYPES:
            payload = obj
        if payload:
            timestamp = timestamp or payload.get("timestamp")
            parent_id = payload.get("parent_thread_id") or payload.get("forked_from_id")
            cwd = payload.get("cwd")
            model = payload.get("model") or payload.get("model_provider")
            metadata["root_id"] = payload.get("session_id")
            role = payload.get("role") if isinstance(payload.get("role"), str) else None
            item_type = str(payload.get("type") or obj.get("type") or "event")
            for kind, name, text in text_from_content(payload.get("content")):
                blocks.append((kind if kind != "text" else item_type, name, text, role))
            for key in ("arguments", "output", "message", "summary", "replacement_history"):
                if key in payload:
                    for kind, name, text in text_from_content({"type": item_type, key: payload[key], "name": payload.get("name")}):
                        blocks.append((kind, name or payload.get("name"), text, role))
            # Freeform/code-mode calls use input, not arguments. Append rather
            # than reorder existing blocks so prior source coordinates stay valid.
            if item_type in {"function_call", "custom_tool_call"} and payload.get("arguments") is None:
                for kind, name, text in text_from_content(
                    {"type": item_type, "input": payload.get("input"), "name": payload.get("name")}
                ):
                    blocks.append((kind, name, text, role))
        return session_id, parent_id, cwd, model, blocks, {"timestamp": timestamp, **metadata}

    if message:
        role = message.get("role") if isinstance(message.get("role"), str) else None
        model = message.get("model") if isinstance(message.get("model"), str) else None
        for kind, name, text in text_from_content(message.get("content")):
            blocks.append((kind, name, text, role))
    cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) else None
    parent_id = obj.get("parentUuid") if isinstance(obj.get("parentUuid"), str) else None
    if obj.get("toolUseResult") is not None:
        for kind, name, text in text_from_content(
            {"type": "tool_result", "result": obj.get("toolUseResult")}
        ):
            blocks.append((kind, name, text, "tool"))
    if obj.get("summary") and isinstance(obj.get("summary"), str):
        blocks.append(("summary", None, str(obj["summary"]), "system"))
    return session_id, parent_id, cwd, model, blocks, {"timestamp": timestamp}


def prefix_sha256(path: pathlib.Path, amount: int = 65536) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(amount))
    return digest.hexdigest()


def indexable_jsonl(stage: pathlib.Path) -> Iterator[Tuple[str, pathlib.Path, str]]:
    roots = [
        ("claude", stage / "data" / "claude" / "projects", stage / "data" / "claude"),
        ("claude", stage / "data" / "hook_inbox" / "claude", stage / "data" / "hook_inbox"),
        ("codex", stage / "data" / "codex" / "sessions", stage / "data" / "codex"),
        ("codex", stage / "data" / "codex" / "archived_sessions", stage / "data" / "codex"),
    ]
    for harness, root, relative_base in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if path.is_file() and ".sync-conflict-" not in path.name:
                yield harness, path, path.relative_to(relative_base).as_posix()


def purge_excluded_sessions(config: Dict[str, Any], conn: sqlite3.Connection) -> int:
    """Remove derived search material if an exclusion is added after indexing."""
    targets = [
        row for row in conn.execute("SELECT * FROM sessions")
        if workspace_is_excluded(config, row["cwd"])
    ]
    for row in targets:
        key = (row["harness"], row["session_id"])
        block_ids = conn.execute(
            "SELECT block_id FROM blocks WHERE harness=? AND session_id=?", key
        ).fetchall()
        conn.executemany("DELETE FROM blocks_fts WHERE block_id=?", block_ids)
        relpaths = {str(row["source_relpath"] or "")}
        relpaths.update(str(item[0]) for item in conn.execute(
            "SELECT DISTINCT source_relpath FROM reasoning_occurrences "
            "WHERE harness=? AND session_id=?", key
        ))
        conn.execute("DELETE FROM blocks WHERE harness=? AND session_id=?", key)
        conn.execute("DELETE FROM references_found WHERE harness=? AND session_id=?", key)
        conn.execute("DELETE FROM summary_jobs WHERE harness=? AND session_id=?", key)
        conn.execute("DELETE FROM reasoning_occurrences WHERE harness=? AND session_id=?", key)
        for relpath in filter(None, relpaths):
            conn.execute(
                "DELETE FROM indexed_files WHERE harness=? AND relpath=?",
                (row["harness"], relpath),
            )
            conn.execute(
                "DELETE FROM reasoning_scan_state WHERE harness=? AND relpath=?",
                (row["harness"], relpath),
            )
        conn.execute("DELETE FROM sessions WHERE harness=? AND session_id=?", key)
        if row["capsule_path"]:
            with contextlib.suppress(OSError):
                pathlib.Path(row["capsule_path"]).unlink()
    orphans = conn.execute(
        "SELECT payload_id FROM reasoning_payloads p WHERE NOT EXISTS "
        "(SELECT 1 FROM reasoning_occurrences o WHERE o.payload_id=p.payload_id)"
    ).fetchall()
    conn.executemany("DELETE FROM reasoning_fts WHERE rowid=?", orphans)
    conn.executemany("DELETE FROM reasoning_payloads WHERE payload_id=?", orphans)
    conn.commit()
    return len(targets)


def insert_reference(
    conn: sqlite3.Connection,
    harness: str,
    session_id: str,
    block_id: int,
    text: str,
    current_capture: str,
) -> None:
    values: List[Tuple[str, str]] = []
    for match in GITHUB_PR_RE.finditer(text):
        values.append(("github_pr", match.group(0)))
    for match in GITHUB_ISSUE_RE.finditer(text):
        values.append(("github_issue", match.group(0)))
    for match in COMMIT_RE.finditer(text):
        value = match.group(0)
        if len(value) >= 12:
            values.append(("commit", value.lower()))
    for kind, value in values:
        conn.execute(
            """
            INSERT INTO references_found(harness,session_id,kind,value,first_block_id,last_seen_capture)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(harness,session_id,kind,value) DO UPDATE SET
                last_seen_capture=excluded.last_seen_capture
            """,
            (harness, session_id, kind, value, block_id, current_capture),
        )


def index_capture(
    config: Dict[str, Any],
    conn: sqlite3.Connection,
    stage: pathlib.Path,
    current_capture: str,
    inventory_rows: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Append evidence and backfill old tool calls atomically, retaining hit IDs."""
    with conn:
        stats = {"files_scanned": 0, "files_backfilled": 0, "tool_calls_added": 0}
        hashes = {
            (row["harness"], row["relpath"]): row.get("sha256")
            for row in inventory_rows
            if row["change_kind"] != "deleted"
        }
        changed_sessions: set = set()
        session_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for harness, path, relative in indexable_jsonl(stage):
            info = path.stat()
            prefix = prefix_sha256(path)
            prior = conn.execute(
                "SELECT * FROM indexed_files WHERE harness=? AND relpath=?",
                (harness, relative),
            ).fetchone()
            upgrade_tools = bool(prior and prior["tool_calls_version"] < TOOL_CALLS_VERSION)
            if not upgrade_tools and prior and prior["size_bytes"] == info.st_size and prior["mtime_ns"] == info.st_mtime_ns:
                continue
            stats["files_scanned"] += 1
            if upgrade_tools:
                stats["files_backfilled"] += 1
            offset = 0
            line_no = 0
            initial_scan = True
            backfill_until = 0
            if (
                prior
                and prior["prefix_sha256"] == prefix
                and info.st_size >= prior["byte_offset"]
            ):
                initial_scan = False
                if upgrade_tools:
                    backfill_until = int(prior["byte_offset"])
                else:
                    offset = int(prior["byte_offset"])
                    line_no = int(prior["line_no"])
            cutoff = str(config["sources"][harness].get("legacy_cutoff_utc") or "")
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
                        if not isinstance(obj, dict):
                            continue
                    except Exception:
                        continue
                    session_id, parent_id, cwd, model, blocks, metadata = extract_event(
                        harness, relative, obj
                    )
                    timestamp = metadata.get("timestamp")
                    # Replay only tool calls in a previously indexed prefix. Calls
                    # predating a legacy cutoff also belong in this complete tool
                    # index; other old content stays in its historical index.
                    tools_only = safe_offset <= backfill_until or (
                        initial_scan and cutoff and not after_cutoff(timestamp, cutoff)
                    )
                    if tools_only and not any(block[0] == "tool_call" for block in blocks):
                        continue
                    key = (harness, session_id)
                    state = session_state.get(key)
                    if state is None:
                        existing = conn.execute(
                            "SELECT * FROM sessions WHERE harness=? AND session_id=?",
                            key,
                        ).fetchone()
                        tool_counts = {}
                        if existing and existing["tool_counts_json"]:
                            with contextlib.suppress(Exception):
                                tool_counts = json.loads(existing["tool_counts_json"])
                        state = {
                            "parent_id": parent_id or (existing["parent_id"] if existing else None),
                            "root_id": metadata.get("root_id") or (existing["root_id"] if existing else None),
                            "cwd": cwd or (existing["cwd"] if existing else None),
                            "model": model or (existing["model"] if existing else None),
                            "started_utc": existing["started_utc"] if existing else None,
                            "ended_utc": existing["ended_utc"] if existing else None,
                            "first_prompt": existing["first_prompt"] if existing else None,
                            "latest_user": existing["latest_user"] if existing else None,
                            "latest_assistant": existing["latest_assistant"] if existing else None,
                            "tool_counts": tool_counts,
                            "source_relpath": relative,
                        }
                        session_state[key] = state
                    if timestamp:
                        if not state["started_utc"] or str(timestamp) < str(state["started_utc"]):
                            state["started_utc"] = str(timestamp)
                        if not state["ended_utc"] or str(timestamp) > str(state["ended_utc"]):
                            state["ended_utc"] = str(timestamp)
                    state["cwd"] = cwd or state["cwd"]
                    state["model"] = model or state["model"]
                    state["source_relpath"] = relative
                    for block_index, (kind, tool_name, text, role) in enumerate(blocks):
                        if tools_only and kind != "tool_call":
                            continue
                        if not text or not text.strip():
                            continue
                        digest = utils.sha256_text(text)
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO blocks(
                                harness,session_id,source_relpath,line_no,block_index,timestamp,
                                role,kind,tool_name,text,text_sha256,capture_id
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                harness,
                                session_id,
                                relative,
                                line_no,
                                block_index,
                                timestamp,
                                role,
                                kind,
                                tool_name,
                                text,
                                digest,
                                current_capture,
                            ),
                        )
                        if cursor.rowcount != 1:
                            continue
                        if kind == "tool_call":
                            stats["tool_calls_added"] += 1
                        block_id = int(cursor.lastrowid)
                        conn.execute(
                            "INSERT INTO blocks_fts VALUES(?,?,?,?,?,?,?,?,?)",
                            (
                                text,
                                harness,
                                session_id,
                                relative,
                                line_no,
                                block_id,
                                role,
                                kind,
                                tool_name,
                            ),
                        )
                        insert_reference(conn, harness, session_id, block_id, text, current_capture)
                        changed_sessions.add(key)
                        if role == "user" and kind not in {"tool_result", "tool_call"}:
                            if not state["first_prompt"]:
                                state["first_prompt"] = utils.compact(text, 4000)
                            state["latest_user"] = utils.compact(text, 4000)
                        elif role == "assistant":
                            state["latest_assistant"] = utils.compact(text, 5000)
                        if kind == "tool_call" or tool_name:
                            name = tool_name or "unknown"
                            state["tool_counts"][name] = int(state["tool_counts"].get(name, 0)) + 1
            conn.execute(
                """
                INSERT INTO indexed_files(harness,relpath,size_bytes,mtime_ns,byte_offset,line_no,prefix_sha256,last_capture,tool_calls_version)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(harness,relpath) DO UPDATE SET
                    size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                    byte_offset=excluded.byte_offset,line_no=excluded.line_no,
                    prefix_sha256=excluded.prefix_sha256,last_capture=excluded.last_capture,
                    tool_calls_version=excluded.tool_calls_version
                """,
                (harness, relative, info.st_size, info.st_mtime_ns, safe_offset, line_no, prefix,
                 current_capture, TOOL_CALLS_VERSION),
            )
        for (harness, session_id), state in session_state.items():
            source_hash = hashes.get((harness, state["source_relpath"]))
            conn.execute(
                """
                INSERT INTO sessions(
                    harness,session_id,parent_id,root_id,cwd,model,started_utc,ended_utc,
                    first_prompt,latest_user,latest_assistant,tool_counts_json,source_relpath,
                    source_sha256,updated_capture
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(harness,session_id) DO UPDATE SET
                    parent_id=COALESCE(excluded.parent_id,sessions.parent_id),
                    root_id=COALESCE(excluded.root_id,sessions.root_id),
                    cwd=COALESCE(excluded.cwd,sessions.cwd),
                    model=COALESCE(excluded.model,sessions.model),
                    started_utc=COALESCE(sessions.started_utc,excluded.started_utc),
                    ended_utc=COALESCE(excluded.ended_utc,sessions.ended_utc),
                    first_prompt=COALESCE(sessions.first_prompt,excluded.first_prompt),
                    latest_user=COALESCE(excluded.latest_user,sessions.latest_user),
                    latest_assistant=COALESCE(excluded.latest_assistant,sessions.latest_assistant),
                    tool_counts_json=excluded.tool_counts_json,
                    source_relpath=excluded.source_relpath,
                    source_sha256=excluded.source_sha256,
                    updated_capture=excluded.updated_capture
                """,
                (
                    harness,
                    session_id,
                    state["parent_id"],
                    state["root_id"],
                    state["cwd"],
                    state["model"],
                    state["started_utc"],
                    state["ended_utc"],
                    state["first_prompt"],
                    state["latest_user"],
                    state["latest_assistant"],
                    json.dumps(state["tool_counts"], sort_keys=True),
                    state["source_relpath"],
                    source_hash,
                    current_capture,
                ),
            )
        for key in changed_sessions:
            reports.generate_capsule(config, conn, key[0], key[1], current_capture)
    return stats
