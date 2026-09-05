"""Bounded, balanced search across incremental and historical indexes."""

from __future__ import annotations

import collections
import contextlib
import pathlib
import re
import reasoning_index
import sqlite3
from privacy_filter import transcript_is_excluded
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import catalog
from .common import (
    UUID_RE,
)


def normalized_fts_query(query: str) -> str:
    """Turn ordinary punctuation-heavy text into safe broad FTS retrieval.

    An explicitly quoted Boolean expression remains available for callers that
    need exact AND/OR semantics. Ordinary prose is tokenized, quoted, and ORed:
    a broad result that must be inspected is safer than a false no-history
    result or an FTS syntax error that sends an agent back to paid compute.
    """
    query = query.strip()
    atom = r'(?:"(?:[^"]|"")*"|[A-Za-z0-9_][A-Za-z0-9_.:/-]*)'
    boolean_expression = rf'{atom}(?:\s+(?:AND|OR|NOT|NEAR)\s+{atom})+'
    if re.fullmatch(boolean_expression, query):
        parts = re.findall(rf'{atom}|AND|OR|NOT|NEAR', query)
        return " ".join(
            part if part in {"AND", "OR", "NOT", "NEAR"} or part.startswith('"')
            else '"' + part.replace('"', '""') + '"'
            for part in parts
        )
    terms = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*", query)
    if not terms:
        return '"' + query.replace('"', '""') + '"'
    unique_terms = list(dict.fromkeys(terms))
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in unique_terms)


def fts_query_variants(query: str) -> List[str]:
    """Try a high-signal intersection before the broad natural-language fallback."""
    broad = normalized_fts_query(query)
    if re.search(r"\s(?:AND|OR|NOT|NEAR)\s", query):
        return [broad]
    terms = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*", query)))
    stopwords = {
        "a", "an", "and", "for", "from", "in", "is", "latest", "of", "on",
        "or", "stable", "the", "to", "versus", "vs", "with",
    }
    signal = [term for term in terms if term.casefold() not in stopwords]
    if len(signal) < 2:
        return [broad]
    strict = " AND ".join('"' + term.replace('"', '""') + '"' for term in signal)
    return [strict, broad] if strict != broad else [broad]


def balanced_search_results(results: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Rank match quality first, prefer raw evidence, then diversify harnesses."""
    if limit <= 0:
        return []
    if any(int(item.get("match_tier", 1)) == 0 for item in results) and any(
        int(item.get("match_tier", 1)) > 0 for item in results
    ):
        # Broad OR retrieval exists to prevent false no-hits. Once the
        # high-signal intersection returns evidence, broad matches add tokens
        # and distractors rather than useful recall. A caller can issue a
        # shorter synonym query or explicit OR when expansion is intentional.
        results = [item for item in results if int(item.get("match_tier", 1)) == 0]
    compact: List[Dict[str, Any]] = []
    seen_units: set[Tuple[str, str, str]] = set()
    seen_excerpts: set[str] = set()
    for item in results:
        unit = str(
            item.get("session_id") or item.get("path") or item.get("source_relpath")
            or item.get("relpath") or item.get("database") or item.get("excerpt") or ""
        )
        key = (
            str(item.get("harness") or "unknown"),
            str(item.get("source") or "unknown"),
            unit,
        )
        if key in seen_units:
            continue
        excerpt_key = re.sub(r"\W+", " ", str(item.get("excerpt") or "").casefold()).strip()
        if excerpt_key and excerpt_key in seen_excerpts:
            continue
        seen_units.add(key)
        if excerpt_key:
            seen_excerpts.add(excerpt_key)
        compact.append(item)
    results = compact
    balanced: List[Dict[str, Any]] = []
    tiers = sorted({int(item.get("match_tier", 1)) for item in results})
    for tier in tiers:
        # Summaries route an investigation but do not displace transcript
        # evidence. They fill only when all raw buckets for this match tier are
        # exhausted.
        for source_phase in (("incremental", "historical"), ("summary",)):
            buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = collections.defaultdict(list)
            for item in results:
                if (
                    int(item.get("match_tier", 1)) == tier
                    and str(item.get("source") or "unknown") in source_phase
                ):
                    buckets[(str(item.get("harness") or "unknown"), str(item.get("source") or "unknown"))].append(item)
            preferred = [
                (harness, source)
                for source in source_phase
                for harness in ("claude", "codex")
            ]
            order = [key for key in preferred if key in buckets]
            order.extend(sorted(key for key in buckets if key not in order))
            positions = {key: 0 for key in order}
            while len(balanced) < limit:
                emitted = False
                for key in order:
                    position = positions[key]
                    if position >= len(buckets[key]):
                        continue
                    balanced.append(buckets[key][position])
                    positions[key] += 1
                    emitted = True
                    if len(balanced) >= limit:
                        break
                if not emitted:
                    break
            if len(balanced) >= limit:
                break
        if len(balanced) >= limit:
            break
    return balanced


def legacy_claude_raw_path(database: pathlib.Path, relpath: str) -> Optional[pathlib.Path]:
    """Resolve a legacy Claude index row to its captured plaintext source."""
    try:
        workspace = database.resolve().parents[2]
    except IndexError:
        return None
    snapshot_root = workspace / "source_snapshot"
    snapshots = sorted(snapshot_root.glob("claude_home_*"), reverse=True)
    for snapshot in snapshots:
        for candidate in (snapshot / "projects" / relpath, snapshot / relpath):
            if candidate.is_file():
                return candidate.resolve()
    candidate = workspace / relpath
    return candidate.resolve() if candidate.is_file() else None


def legacy_codex_raw_path(database: pathlib.Path, raw_path: str) -> pathlib.Path:
    """Resolve a legacy Codex source after its archive tree moves hosts."""
    original = pathlib.Path(raw_path).expanduser()
    if original.is_file():
        return original.resolve()
    try:
        workspace = database.resolve().parents[2]
    except IndexError:
        return original
    parts = original.parts
    for marker in ("source_snapshot", "extracted"):
        if marker not in parts:
            continue
        candidate = workspace.joinpath(*parts[parts.index(marker):])
        if candidate.is_file():
            return candidate.resolve()
    return original


def search_archive(
    config: Dict[str, Any], query: str, limit: int = 8, reasoning_only: bool = False,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    matches = fts_query_variants(query)
    seen: set[Tuple[Any, ...]] = set()

    def append_unique(item: Dict[str, Any]) -> None:
        key = (
            item.get("source"), item.get("harness"), item.get("session_id"),
            item.get("source_relpath"), item.get("path"), item.get("database"),
            item.get("line_no"), item.get("excerpt"),
        )
        if key not in seen:
            seen.add(key)
            results.append(item)

    conn = catalog.connect_catalog_readonly(config)
    try:
        exact_session = query.strip()
        if re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            exact_session,
        ):
            for harness in ("claude", "codex"):
                root = pathlib.Path(config["sources"][harness]["root"]).expanduser().resolve()
                search_roots = (
                    (root / "projects",) if harness == "claude"
                    else (root / "sessions", root / "archived_sessions")
                )
                paths: List[pathlib.Path] = []
                for search_root in search_roots:
                    if search_root.is_dir():
                        paths.extend(search_root.rglob(f"*{exact_session}*.jsonl"))
                for path in sorted(set(paths))[:limit]:
                    if transcript_is_excluded(config, path):
                        continue
                    relative = str(path.relative_to(root))
                    item = {
                        "source": "incremental", "match_tier": -1,
                        "match_mode": "session-id", "harness": harness,
                        "session_id": exact_session, "source_relpath": relative,
                        "line_no": None,
                        "excerpt": "exact raw transcript filename match",
                        "raw_path": str(path),
                    }
                    append_unique(item)
            if results:
                return results[:limit]
        for match_tier, match in enumerate(matches):
            match_mode = "all-signal" if len(matches) > 1 and match_tier == 0 else "broad"
            if len(matches) == 1 and re.search(r"\s(?:AND|OR|NOT|NEAR)\s", query):
                match_mode = "explicit"
            for harness in ("claude", "codex"):
                for row in conn.execute(
                    """
                    SELECT harness,session_id,source_relpath,line_no,block_id,role,kind,tool_name,
                           snippet(blocks_fts,0,'[',']','…',18) AS excerpt,
                           bm25(blocks_fts) AS bm25_score
                    FROM blocks_fts WHERE blocks_fts MATCH ? AND harness=?
                      AND (?=0 OR kind IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
                    ORDER BY bm25(blocks_fts) LIMIT ?
                    """,
                    (match, harness, int(reasoning_only), limit),
                ):
                    item = {
                        "source": "incremental", "match_tier": match_tier,
                        "match_mode": match_mode, **dict(row),
                    }
                    item["hit_id"] = f"i:{harness}:{row['block_id']}"
                    item["raw_path"] = str(
                        pathlib.Path(config["sources"][harness]["root"]) / row["source_relpath"]
                    )
                    append_unique(item)
                if harness == "codex":
                    for row in reasoning_index.search(conn, match, limit):
                        item = {
                            "source": "incremental", "harness": "codex",
                            "role": "reasoning", "match_tier": match_tier,
                            "match_mode": match_mode, **row,
                        }
                        item["hit_id"] = f"r:codex:{row['payload_id']}"
                        item["raw_path"] = str(
                            pathlib.Path(config["sources"]["codex"]["root"])
                            / row["source_relpath"]
                        )
                        append_unique(item)
                if not reasoning_only:
                    for row in conn.execute(
                        """
                        SELECT harness,path,title,document_id,
                               snippet(documents_fts,0,'[',']','…',18) AS excerpt,
                               bm25(documents_fts) AS bm25_score
                        FROM documents_fts WHERE documents_fts MATCH ? AND harness=?
                        ORDER BY bm25(documents_fts) LIMIT ?
                        """,
                        (match, harness, limit),
                    ):
                        item = {
                            "source": "summary", "match_tier": match_tier,
                            "match_mode": match_mode, **dict(row),
                        }
                        item["hit_id"] = f"s:{row['document_id']}"
                        append_unique(item)
    finally:
        conn.close()
    for legacy in config.get("legacy_indexes", []):
        database = pathlib.Path(legacy["database"]).expanduser()
        if not database.is_file():
            continue
        legacy_conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        legacy_conn.row_factory = sqlite3.Row
        try:
            if legacy["harness"] == "claude":
                sql = """
                    SELECT blocks_fts.rowid AS block_id,b.relpath,b.line_no,b.role,
                           b.block_type AS kind,b.tool_name,
                           snippet(blocks_fts,0,'[',']','…',18) AS excerpt
                    FROM blocks_fts JOIN blocks b ON b.block_id=blocks_fts.rowid
                    WHERE blocks_fts MATCH ?
                      AND (?=0 OR b.block_type IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
                    ORDER BY bm25(blocks_fts) LIMIT ?
                """
            else:
                sql = """
                    SELECT b.block_id,b.file_thread_id AS session_id,b.timestamp,b.role,
                           b.kind,b.name AS tool_name,e.line_no,f.source_path AS raw_path,
                           snippet(blocks_fts,0,'[',']','…',18) AS excerpt
                    FROM blocks_fts
                    JOIN blocks b ON b.block_id=blocks_fts.rowid
                    JOIN events e ON e.event_id=b.event_id
                    JOIN files f ON f.file_id=b.file_id
                    WHERE blocks_fts MATCH ?
                      AND (?=0 OR b.kind IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
                    ORDER BY bm25(blocks_fts) LIMIT ?
                """
            for match_tier, match in enumerate(matches):
                match_mode = "all-signal" if len(matches) > 1 and match_tier == 0 else "broad"
                if len(matches) == 1 and re.search(r"\s(?:AND|OR|NOT|NEAR)\s", query):
                    match_mode = "explicit"
                for row in legacy_conn.execute(sql, (match, int(reasoning_only), limit)):
                    item = {
                        "source": "historical",
                        "match_tier": match_tier,
                        "match_mode": match_mode,
                        "harness": legacy["harness"],
                        "database": str(database),
                        **dict(row),
                    }
                    item["hit_id"] = f"l:{legacy['harness']}:{row['block_id']}"
                    if legacy["harness"] == "claude":
                        raw_path = legacy_claude_raw_path(database, str(row["relpath"]))
                        if raw_path:
                            item["raw_path"] = str(raw_path)
                    elif row["raw_path"]:
                        item["raw_path"] = str(
                            legacy_codex_raw_path(database, str(row["raw_path"]))
                        )
                    append_unique(item)
        except sqlite3.OperationalError:
            # A literal fallback is friendlier for punctuation-heavy IDs.
            quoted = '"' + query.replace('"', '""') + '"'
            with contextlib.suppress(sqlite3.OperationalError):
                for row in legacy_conn.execute(sql, (quoted, int(reasoning_only), limit)):
                    item = {
                        "source": "historical",
                        "match_tier": len(matches),
                        "match_mode": "literal-fallback",
                        "harness": legacy["harness"],
                        "database": str(database),
                        **dict(row),
                    }
                    item["hit_id"] = f"l:{legacy['harness']}:{row['block_id']}"
                    if legacy["harness"] == "claude":
                        raw_path = legacy_claude_raw_path(database, str(row["relpath"]))
                        if raw_path:
                            item["raw_path"] = str(raw_path)
                    elif row["raw_path"]:
                        item["raw_path"] = str(
                            legacy_codex_raw_path(database, str(row["raw_path"]))
                        )
                    append_unique(item)
        finally:
            legacy_conn.close()
    return balanced_search_results(results, limit)


def terse_search_coordinate(item: Dict[str, Any]) -> str:
    """Route a terse hit without repeating long absolute paths in stage one."""
    raw = str(
        item.get("raw_path") or item.get("path") or item.get("source_relpath")
        or item.get("relpath") or item.get("session_id") or ""
    )
    if item.get("match_mode") == "session-id":
        coordinate = raw
    else:
        session = str(item.get("session_id") or "")
        match = UUID_RE.search(session) or UUID_RE.search(raw)
        coordinate = match.group(0) if match else pathlib.Path(raw).name or raw
    if item.get("line_no"):
        coordinate = f"{coordinate}:{item['line_no']}"
    return coordinate
