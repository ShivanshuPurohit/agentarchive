"""Bounded expansion of a selected indexed evidence coordinate."""

from __future__ import annotations

import pathlib
import re
import hashlib
import reasoning_index
from privacy_filter import transcript_is_excluded, workspace_is_excluded
from typing import Any, Dict

from . import catalog, search


def verified_live_legacy_path(config, harness, original, relative, line_no, digest):
    """Reuse a known live coordinate only when its raw line matches the archive."""
    root = pathlib.Path(config["sources"][harness]["root"]).expanduser().resolve()
    if harness == "claude":
        candidates = [root / "projects" / relative, root / relative]
    else:
        parts = pathlib.Path(original).parts
        candidates = [root.joinpath(*parts[parts.index(marker):]) for marker in
                      ("sessions", "archived_sessions") if marker in parts]
    for candidate in candidates:
        candidate = candidate.resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file() or transcript_is_excluded(config, candidate):
            continue
        with candidate.open("rb") as handle:
            for current, raw in enumerate(handle, 1):
                if current == line_no:
                    if digest in {hashlib.sha256(raw).hexdigest(), hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()}:
                        return candidate
                    break
    return None


def indexed_context(
    config: Dict[str, Any], coordinate: str, before: int = 0, after: int = 0,
    max_chars: int = 6000,
) -> str:
    """Return one selected hit, or indexed PATH:LINE blocks, under a hard cap."""
    if before < 0 or after < 0:
        raise ValueError("context before/after must be non-negative")
    if not 256 <= max_chars <= 100_000:
        raise ValueError("context max-chars must be between 256 and 100000")
    conn = catalog.connect_catalog_readonly(config)
    try:
        summary_match = re.fullmatch(r"s:([1-9][0-9]*)", coordinate)
        reasoning_match = re.fullmatch(r"r:codex:([1-9][0-9]*)", coordinate)
        hit_match = re.fullmatch(r"i:(claude|codex):([1-9][0-9]*)", coordinate)
        legacy_match = re.fullmatch(r"l:(claude|codex):([1-9][0-9]*)", coordinate)
        if reasoning_match:
            if before or after:
                raise ValueError("normalized reasoning context does not support before/after")
            evidence = reasoning_index.context(conn, int(reasoning_match.group(1)))
            if not evidence:
                raise ValueError("reasoning hit ID is absent from the current catalog")
            payload = evidence["payload"]
            pieces = [
                f"[reasoning payload sha256={payload['text_sha256']} "
                f"occurrences={evidence['occurrence_count']}]\n{payload['text'].strip()}\n"
            ]
            codex_root = pathlib.Path(config["sources"]["codex"]["root"]).expanduser().resolve()
            for occurrence in evidence["occurrences"]:
                pieces.append(
                    f"[source {codex_root / occurrence['source_relpath']}:{occurrence['line_no']} "
                    f"session={occurrence['session_id']} kind={occurrence['kind']} "
                    f"capture={occurrence['capture_id']}]\n"
                )
            if evidence["occurrence_count"] > len(evidence["occurrences"]):
                pieces.append(
                    f"[additional_occurrences="
                    f"{evidence['occurrence_count'] - len(evidence['occurrences'])}]\n"
                )
            source_label = coordinate
            row_count = 1
        elif summary_match:
            if before or after:
                raise ValueError("summary context does not support before/after")
            row = conn.execute(
                "SELECT document_id,harness,path,title,sha256,text FROM documents WHERE document_id=?",
                (int(summary_match.group(1)),),
            ).fetchone()
            if not row:
                raise ValueError("summary hit ID is absent from the current catalog")
            pieces = [
                f"{row['path']} [summary routing material; sha256={row['sha256']}]\n"
                f"title: {row['title'] or '-'}\n{str(row['text'] or '').strip()}\n"
            ]
            source_label = coordinate
            row_count = 1
        elif legacy_match:
            if before or after:
                raise ValueError("historical context does not support before/after")
            harness = legacy_match.group(1)
            definition = next(
                (item for item in config.get("legacy_indexes", []) if item.get("harness") == harness),
                None,
            )
            if not definition:
                raise ValueError("historical index is not configured for that hit ID")
            database = pathlib.Path(definition["database"]).expanduser().resolve()
            legacy_conn = catalog.open_readonly(database)
            try:
                if harness == "claude":
                    row = legacy_conn.execute(
                        """
                        SELECT b.block_id,b.relpath,b.line_no,b.block_index,b.role,
                               b.block_type AS kind,b.tool_name,b.text,b.text_sha256,
                               e.raw_sha256,e.timestamp
                        FROM blocks b JOIN events e ON e.event_id=b.event_id
                        WHERE b.block_id=?
                        """,
                        (int(legacy_match.group(2)),),
                    ).fetchone()
                    raw_path = search.legacy_claude_raw_path(database, str(row["relpath"])) if row else None
                    coordinate_label = str(raw_path or row["relpath"]) if row else ""
                else:
                    row = legacy_conn.execute(
                        """
                        SELECT b.block_id,e.line_no,0 AS block_index,b.role,b.kind,b.name AS tool_name,
                               b.text,b.text_sha256,e.raw_sha256,b.timestamp,f.source_path AS raw_path
                        FROM blocks b
                        JOIN events e ON e.event_id=b.event_id
                        JOIN files f ON f.file_id=b.file_id
                        WHERE b.block_id=?
                        """,
                        (int(legacy_match.group(2)),),
                    ).fetchone()
                    raw_path = search.legacy_codex_raw_path(database, str(row["raw_path"])) if row else None
                    coordinate_label = str(raw_path) if raw_path else ""
                if not row:
                    raise ValueError("historical hit ID is absent from the configured index")
                columns = {item[1] for item in legacy_conn.execute("PRAGMA table_info(files)")}
                if "cwd" in columns:
                    owner = legacy_conn.execute("SELECT f.cwd FROM files f JOIN blocks b ON b.file_id=f.file_id "
                                                "WHERE b.block_id=?", (int(legacy_match.group(2)),)).fetchone()
                    if owner and workspace_is_excluded(config, owner[0]):
                        raise ValueError("context belongs to an excluded workspace")
                if raw_path and transcript_is_excluded(config, pathlib.Path(raw_path)):
                    raise ValueError("context belongs to an excluded workspace")
                if not raw_path or not pathlib.Path(raw_path).is_file():
                    live = verified_live_legacy_path(config, harness, coordinate_label,
                                                    row["relpath"] if harness == "claude" else "",
                                                    row["line_no"], row["raw_sha256"])
                    if live:
                        coordinate_label = str(live)
                pieces = [
                    f"{coordinate_label}:{row['line_no']}#{row['block_index']} "
                    f"[{row['role'] or '-'}/{row['kind'] or '-'}/{row['tool_name'] or '-'}]\n"
                    f"{str(row['text'] or '').strip()}\n"
                    f"[block_id={row['block_id']} raw_sha256={row['raw_sha256']} "
                    f"text_sha256={row['text_sha256']}]\n"
                ]
                source_label = coordinate
                row_count = 1
            finally:
                legacy_conn.close()
        else:
            if hit_match:
                harness = hit_match.group(1)
                selected = conn.execute(
                    """
                    SELECT block_id,source_relpath,line_no,block_index,timestamp,role,kind,
                           tool_name,text,text_sha256,capture_id
                    FROM blocks WHERE harness=? AND block_id=?
                    """,
                    (harness, int(hit_match.group(2))),
                ).fetchone()
                if not selected:
                    raise ValueError("incremental hit ID is absent from the current catalog")
                relative = selected["source_relpath"]
                line_no = int(selected["line_no"])
                path = (
                    pathlib.Path(config["sources"][harness]["root"]).expanduser().resolve()
                    / relative
                )
                if before or after:
                    rows = conn.execute(
                        """
                        SELECT block_id,line_no,block_index,timestamp,role,kind,tool_name,
                               text,text_sha256,capture_id
                        FROM blocks
                        WHERE harness=? AND source_relpath=? AND line_no BETWEEN ? AND ?
                        ORDER BY line_no,block_index
                        """,
                        (harness, relative, max(1, line_no - before), line_no + after),
                    ).fetchall()
                else:
                    rows = [selected]
                source_label = coordinate
            else:
                try:
                    raw_path, raw_line = coordinate.rsplit(":", 1)
                    line_no = int(raw_line)
                except (ValueError, AttributeError) as exc:
                    raise ValueError(
                        "context target must be i:/l:HARNESS:BLOCK_ID, r:codex:PAYLOAD_ID, "
                        "s:DOCUMENT_ID, or absolute PATH:LINE"
                    ) from exc
                path = pathlib.Path(raw_path).expanduser().resolve()
                if not path.is_absolute() or line_no <= 0:
                    raise ValueError("context coordinate must use an absolute path and positive line")
                harness = ""
                relative = ""
                for candidate, definition in config["sources"].items():
                    root = pathlib.Path(definition["root"]).expanduser().resolve()
                    try:
                        relative = str(path.relative_to(root))
                    except ValueError:
                        continue
                    harness = candidate
                    break
                if not harness:
                    raise ValueError("context path is outside the indexed Claude/Codex source roots")
                rows = conn.execute(
                    """
                    SELECT block_id,line_no,block_index,timestamp,role,kind,tool_name,
                           text,text_sha256,capture_id
                    FROM blocks
                    WHERE harness=? AND source_relpath=? AND line_no BETWEEN ? AND ?
                    ORDER BY line_no,block_index
                    """,
                    (harness, relative, max(1, line_no - before), line_no + after),
                ).fetchall()
                source_label = coordinate
            if not rows:
                raise ValueError("no indexed block exists at that coordinate; inspect the live raw file")
            owners = conn.execute("SELECT cwd FROM sessions WHERE harness=? AND source_relpath=?", (harness, relative))
            if any(workspace_is_excluded(config, owner[0]) for owner in owners) or transcript_is_excluded(config, path):
                raise ValueError("context belongs to an excluded workspace")
            pieces = []
            for row in rows:
                pieces.append(
                    f"{path}:{row['line_no']}#{row['block_index']} "
                    f"[{row['role'] or '-'}/{row['kind'] or '-'}/{row['tool_name'] or '-'}]\n"
                    f"{str(row['text'] or '').strip()}\n"
                    f"[block_id={row['block_id']} capture={row['capture_id']} "
                    f"sha256={row['text_sha256']}]\n"
                )
            row_count = len(rows)
    finally:
        conn.close()
    content = "".join(pieces)
    target = f" target={source_label}" if source_label.startswith(("i:", "l:", "r:", "s:")) else ""
    prefix = f"INDEXED_CONTEXT{target} budget={max_chars} blocks={row_count}\n"
    if len(prefix) + len(content) <= max_chars:
        return prefix + content
    suffix = "\n[TRUNCATED omitted_chars=0]\n"
    available = max(0, max_chars - len(prefix) - len(suffix))
    omitted = max(0, len(content) - available)
    suffix = f"\n[TRUNCATED omitted_chars={omitted}]\n"
    available = max(0, max_chars - len(prefix) - len(suffix))
    omitted = max(0, len(content) - available)
    suffix = f"\n[TRUNCATED omitted_chars={omitted}]\n"
    available = max(0, max_chars - len(prefix) - len(suffix))
    result = prefix + content[:available] + suffix
    return result[:max_chars]
