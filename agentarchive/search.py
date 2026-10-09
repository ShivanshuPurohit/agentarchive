"""Bounded, relevance-ranked search across incremental and historical indexes."""

from __future__ import annotations

import pathlib
import re
import reasoning_index
import sqlite3
from privacy_filter import transcript_is_excluded, workspace_is_excluded
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import catalog, retrieval
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
    """Preserve explicit syntax; strip question words before bounded expansion."""
    if re.search(r"\s(?:AND|OR|NOT|NEAR)\s", query):
        return [normalized_fts_query(query)]
    atoms = [retrieval.atom(term) for term in retrieval.terms(query)]
    if not atoms:
        return []
    strict, broad = " AND ".join(atoms), " OR ".join(atoms)
    if strict == broad:
        return [strict]
    variants = [strict]
    signal = retrieval.terms(query)
    if len(signal) >= 3:
        # Preserve nearby noun/identifier groups when a question adds verbs
        # that the answer does not repeat. No unbounded single-word OR scan.
        quoted = ['"' + word.strip('"').replace('"', '""') + '"' for word in signal]
        # OR-combined NEAR groups can make SQLite's cross-row snippet cursor
        # report SQLITE_CORRUPT_VTAB on a healthy index. Merge bounded results
        # from separate groups instead; keep all groups before broad fallback.
        variants.extend("NEAR(" + " ".join(quoted[i:i + 3]) + ", 12)"
                        for i in range(len(quoted) - 2))
    return variants + [broad]


def balanced_search_results(results: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Compatibility entry point; relevance precedes source diversity."""
    return retrieval.ranked_results(results, limit)


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


def coverage_warnings(config: Dict[str, Any]) -> List[str]:
    """Do not silently turn unavailable history into a negative search result."""
    return [
        f"{entry['harness']} historical index is unavailable: {entry['database']}"
        for entry in config.get("legacy_indexes", [])
        if not pathlib.Path(entry["database"]).expanduser().is_file()
    ]


def _session_matches(
    config: Dict[str, Any], conn: sqlite3.Connection, session_id: str, limit: int,
) -> List[Dict[str, Any]]:
    # The filtered catalog already owns source discovery. Never walk live
    # transcript roots just to resolve a known session UUID.
    rows = conn.execute(
        "SELECT harness,source_relpath,cwd FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchall()
    if not rows:
        harnesses = list(config["sources"])
        rows = conn.execute(
            "SELECT DISTINCT harness,source_relpath,NULL AS cwd FROM blocks WHERE harness IN (" +
            ",".join("?" for _ in harnesses) + ") AND session_id=? LIMIT ?",
            [*harnesses, session_id, limit],
        ).fetchall()
    results = []
    for row in rows:
        if row["harness"] not in config["sources"] or workspace_is_excluded(config, row["cwd"]):
            continue
        path = (pathlib.Path(config["sources"][row["harness"]]["root"]) / row["source_relpath"]).resolve()
        if transcript_is_excluded(config, path):
            continue
        results.append({
            "source": "incremental", "match_tier": -1, "match_mode": "session-id",
            "harness": row["harness"], "session_id": session_id,
            "source_relpath": row["source_relpath"], "line_no": None,
            "excerpt": "exact raw transcript filename match", "raw_path": str(path),
        })
    if not results:
        for entry in config.get("legacy_indexes", []):
            database = pathlib.Path(entry["database"]).expanduser()
            if not database.is_file():
                continue
            legacy = catalog.open_readonly(database)
            try:
                if entry["harness"] == "codex":
                    found = legacy.execute("SELECT * FROM files WHERE thread_id=? LIMIT ?", (session_id, limit))
                else:
                    found = legacy.execute("SELECT * FROM files WHERE relpath LIKE ? LIMIT ?", ("%" + session_id + "%", limit))
                for record in found:
                    item = dict(record)
                    if workspace_is_excluded(config, item.get("cwd")):
                        continue
                    block = legacy.execute("SELECT block_id,line_no FROM blocks WHERE file_id=? ORDER BY block_id LIMIT 1",
                                           (item["file_id"],)).fetchone() if entry["harness"] == "claude" else legacy.execute(
                        "SELECT b.block_id,e.line_no FROM blocks b JOIN events e ON e.event_id=b.event_id "
                        "WHERE b.file_id=? ORDER BY b.block_id LIMIT 1", (item["file_id"],),
                    ).fetchone()
                    if not block:
                        continue
                    raw = (legacy_claude_raw_path(database, item["relpath"]) if entry["harness"] == "claude"
                           else legacy_codex_raw_path(database, item["source_path"]))
                    if raw and transcript_is_excluded(config, raw):
                        continue
                    results.append({"source": "historical", "harness": entry["harness"], "database": str(database),
                                    "session_id": session_id, "match_tier": -1, "match_mode": "session-id",
                                    "hit_id": f"l:{entry['harness']}:{block['block_id']}", "line_no": block["line_no"],
                                    "raw_path": str(raw or item["relpath"]), "excerpt": "exact historical session; indexed context available"})
            finally:
                legacy.close()
    return results[:limit]


def _incremental_candidates(
    config: Dict[str, Any], conn: sqlite3.Connection, match: str, count: int, reasoning_only: bool,
) -> List[Dict[str, Any]]:
    harnesses = list(config["sources"])
    placeholders = ",".join("?" for _ in harnesses)
    rows = conn.execute(
        f"""
        SELECT harness,session_id,source_relpath,line_no,block_id,role,kind,tool_name,
               snippet(blocks_fts,0,'[',']','…',40) AS excerpt,rank AS bm25_score,
               length(text) AS _chars,substr(text,1,3000) AS _prefix
        FROM blocks_fts WHERE blocks_fts MATCH ? AND harness IN ({placeholders})
          AND (?=0 OR kind IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
        ORDER BY rank LIMIT ?
        """, [match, *harnesses, int(reasoning_only), count],
    ).fetchall()
    results = []
    for rank, row in enumerate(rows):
        item = {"source": "incremental", "_rank": rank, **dict(row)}
        item["hit_id"] = f"i:{row['harness']}:{row['block_id']}"
        item["raw_path"] = str(pathlib.Path(config["sources"][row["harness"]]["root"]) / row["source_relpath"])
        results.append(item)
    if rows:
        ids = [row["block_id"] for row in rows]
        metadata = {
            row["block_id"]: dict(row) for row in conn.execute(
                "SELECT block_id,timestamp,text_sha256 FROM blocks WHERE block_id IN (" +
                ",".join("?" for _ in ids) + ")", ids,
            )
        }
        for item in results:
            item.update(metadata.get(item["block_id"], {}))
    if "codex" in config["sources"]:
        for rank, row in enumerate(reasoning_index.search(conn, match, count)):
            item = {"source": "incremental", "harness": "codex", "role": "reasoning", "_rank": rank, **row}
            item["hit_id"] = f"r:codex:{row['payload_id']}"
            item["raw_path"] = str(pathlib.Path(config["sources"]["codex"]["root"]) / row["source_relpath"])
            results.append(item)
    return results


def _historical_candidates(
    config: Dict[str, Any], match: str, count: int, reasoning_only: bool,
) -> List[Dict[str, Any]]:
    results = []
    for legacy in config.get("legacy_indexes", []):
        database = pathlib.Path(legacy["database"]).expanduser().resolve()
        if not database.is_file():
            continue
        conn = catalog.open_readonly(database)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
            workspace = "f.cwd" if "cwd" in columns else "NULL"
            if legacy["harness"] == "claude":
                sql = f"""
                    SELECT b.block_id,b.relpath,b.line_no,b.role,b.block_type AS kind,
                           b.tool_name,b.text_sha256,e.timestamp,{workspace} AS cwd,
                           snippet(blocks_fts,0,'[',']','…',40) AS excerpt,
                           rank AS bm25_score,length(b.text) AS _chars,substr(b.text,1,3000) AS _prefix
                    FROM blocks_fts JOIN blocks b ON b.block_id=blocks_fts.rowid
                    JOIN events e ON e.event_id=b.event_id JOIN files f ON f.file_id=b.file_id
                    WHERE blocks_fts MATCH ?
                      AND (?=0 OR b.block_type IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
                    ORDER BY rank LIMIT ?
                """
            else:
                sql = f"""
                    SELECT b.block_id,b.file_thread_id AS session_id,b.timestamp,b.role,
                           b.kind,b.name AS tool_name,b.text_sha256,e.line_no,f.source_path AS raw_path,{workspace} AS cwd,
                           snippet(blocks_fts,0,'[',']','…',40) AS excerpt,
                           rank AS bm25_score,length(b.text) AS _chars,substr(b.text,1,3000) AS _prefix
                    FROM blocks_fts JOIN blocks b ON b.block_id=blocks_fts.rowid
                    JOIN events e ON e.event_id=b.event_id JOIN files f ON f.file_id=b.file_id
                    WHERE blocks_fts MATCH ?
                      AND (?=0 OR b.kind IN ('thinking','reasoning','reasoning_summary','agent_reasoning'))
                    ORDER BY rank LIMIT ?
                """
            for rank, row in enumerate(conn.execute(sql, (match, int(reasoning_only), count))):
                item = {"source": "historical", "harness": legacy["harness"],
                        "database": str(database), "_rank": rank, **dict(row)}
                item["hit_id"] = f"l:{legacy['harness']}:{row['block_id']}"
                if legacy["harness"] == "claude":
                    path = legacy_claude_raw_path(database, str(row["relpath"]))
                    if path:
                        item["raw_path"] = str(path)
                    session = UUID_RE.search(str(row["relpath"]))
                    if session:
                        item["session_id"] = session.group(0)
                elif row["raw_path"]:
                    item["raw_path"] = str(legacy_codex_raw_path(database, str(row["raw_path"])))
                results.append(item)
        finally:
            conn.close()
    return results


def _summary_candidates(conn: sqlite3.Connection, match: str, count: int) -> List[Dict[str, Any]]:
    return [
        {"source": "summary", "hit_id": f"s:{row['document_id']}", "_rank": rank, **dict(row)}
        for rank, row in enumerate(conn.execute(
            """
            SELECT harness,path,title,document_id,
                   snippet(documents_fts,0,'[',']','…',40) AS excerpt,rank AS bm25_score,
                   length(text) AS _chars,substr(text,1,3000) AS _prefix
            FROM documents_fts WHERE documents_fts MATCH ? ORDER BY rank LIMIT ?
            """, (match, count),
        ))
    ]


def search_archive(
    config: Dict[str, Any], query: str, limit: int = 8, reasoning_only: bool = False,
) -> List[Dict[str, Any]]:
    if not query.strip() or limit <= 0:
        return []
    if len(query) > 1024 or limit > 100:
        raise ValueError("search supports at most 1024 query characters and 100 results")
    matches = fts_query_variants(query)
    query_terms = [term for term in retrieval.terms(query) if term not in {"AND", "OR", "NOT", "NEAR"}]
    results: Dict[str, Dict[str, Any]] = {}
    # Fetch enough candidates BEFORE collapsing inherited and repeated blocks.
    candidate_count = min(512, max(64, limit * 16))
    conn = catalog.connect_catalog_readonly(config)
    try:
        exact_session = query.strip()
        if UUID_RE.fullmatch(exact_session):
            found = _session_matches(config, conn, exact_session.lower(), limit)
            if found:
                return found
        workspaces = {
            (row["harness"], row["session_id"]): row["cwd"]
            for row in conn.execute("SELECT harness,session_id,cwd FROM sessions")
        }
        privacy_cache: Dict[str, bool] = {}

        def append(rows: Sequence[Dict[str, Any]], tier: int, mode: str) -> None:
            for item in rows:
                cwd = item.get("cwd") or workspaces.get((item.get("harness"), item.get("session_id")))
                if workspace_is_excluded(config, cwd):
                    continue
                item["cwd"] = cwd
                path = str(item.get("raw_path") or "")
                if path:
                    if path not in privacy_cache:
                        privacy_cache[path] = transcript_is_excluded(config, pathlib.Path(path))
                    if privacy_cache[path]:
                        continue
                item.update(match_tier=tier, match_mode=mode)
                item["_quality"] = retrieval.passage_quality(item, query_terms, query)
                key = item["hit_id"]
                if key not in results or item["_quality"] > results[key]["_quality"]:
                    results[key] = item

        for tier, match in enumerate(matches):
            mode = "all-signal" if len(matches) > 1 and tier == 0 else "broad"
            if match.startswith("NEAR("):
                mode = "nearby"
            if len(matches) == 1 and re.search(r"\s(?:AND|OR|NOT|NEAR)\s", query):
                mode = "explicit"
            append(_incremental_candidates(config, conn, match, candidate_count, reasoning_only), tier, mode)
            append(_historical_candidates(config, match, candidate_count, reasoning_only), tier, mode)
            ranked = balanced_search_results(list(results.values()), limit)
            # A useful nearby match makes a broad query unnecessary. Incidental
            # co-occurrence in a huge block or an old search result does not.
            if (ranked and ranked[0].get("match_coverage", 0) == 1 and ranked[0]["_quality"] >= 0.9
                    and tier >= len(matches) - 2):
                break
        if not reasoning_only and matches and len(results) < limit:
            append(_summary_candidates(conn, matches[0], candidate_count), 0, "all-signal")
        ranked = balanced_search_results(list(results.values()), limit)
        return [{key: value for key, value in item.items() if not key.startswith("_")} for item in ranked]
    finally:
        conn.close()


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
