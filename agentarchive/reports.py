"""Deterministic session capsules, status, and archive entrypoint documents."""

from __future__ import annotations

import contextlib
import hashlib
import json
import pathlib
import re
import reasoning_index
import shlex
import sqlite3
from typing import Any, Dict

from . import catalog, common as utils, config as configuration, storage
from .common import VERSION


def legacy_entry_points(config: Dict[str, Any]) -> list[str]:
    lines = [
        f"- Historical {entry['harness'].capitalize()} handoff: `{entry['start_here']}`"
        for entry in config.get("legacy_indexes", []) if entry.get("start_here")
    ]
    if config.get("legacy_comparison"):
        lines.append(f"- Harness comparison: `{config['legacy_comparison']}`")
    return lines


def source_quick_counts(config: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for harness, definition in config["sources"].items():
        root = pathlib.Path(definition["root"])
        files = 0
        bytes_total = 0
        for relative in definition.get("paths", []):
            path = root / relative
            if path.is_file():
                files += 1
                bytes_total += path.stat().st_size
            elif path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        files += 1
                        with contextlib.suppress(OSError):
                            bytes_total += child.stat().st_size
        result[harness] = {"files": files, "bytes": bytes_total}
    return result


def render_status(config: Dict[str, Any], conn: sqlite3.Connection) -> None:
    state = configuration.state_root(config)
    latest = catalog.latest_success(conn)
    snapshots = storage.restic(config, ["snapshots", "--json"], check=False)
    snapshot_count = 0
    if snapshots.returncode == 0:
        with contextlib.suppress(Exception):
            snapshot_count = len(json.loads(snapshots.stdout or "[]"))
    pending = len(list((state / "queue").glob("*.json")))
    offsite = config.get("offsite") or {}
    lines = [
        "# Agent archive status",
        "",
        f"Generated: `{utils.utc_now()}`",
        "",
        f"- Local encrypted repository: `{config['local_repository']}`",
        f"- Local snapshots: **{snapshot_count:,}**",
        f"- Encrypted restic off-device repository: "
        f"**{'configured' if offsite.get('enabled') else 'not configured'}**",
        f"- Queued hook requests: **{pending:,}**",
    ]
    reasoning_counts = reasoning_index.stats(conn)
    lines.append(
        "- Normalized readable reasoning: "
        f"**{reasoning_counts['payloads']:,} unique payloads** across "
        f"**{reasoning_counts['occurrences']:,} provenance occurrences**; "
        f"opaque Codex reasoning retained only in raw archives: "
        f"**{reasoning_counts['opaque_items']:,} items**"
    )
    if latest:
        lines += [
            f"- Latest successful capture: `{latest['capture_id']}` ({latest['finished_utc']})",
            f"- Latest restic snapshot: `{latest['restic_snapshot']}`",
            f"- Captured files: **{latest['file_count']:,}**; logical bytes: **{latest['logical_bytes']:,}**",
            f"- Changes: new **{latest['new_files']:,}**, changed **{latest['changed_files']:,}**, deleted **{latest['deleted_files']:,}**",
        ]
    else:
        lines.append("- Latest successful capture: **none yet**")
    health = conn.execute(
        "SELECT checked_utc,check_type,status,detail FROM health_checks ORDER BY check_id DESC LIMIT 10"
    ).fetchall()
    lines += ["", "## Recent health checks", "", "| Time | Check | Status | Detail |", "|---|---|---|---|"]
    for row in health:
        lines.append(
            f"| {row['checked_utc']} | {row['check_type']} | {row['status']} | {utils.md_cell(row['detail'] or '', 180)} |"
        )
    lines += [
        "",
        "## Entry points",
        "",
        "- Unified live index: `START_HERE.md` in this directory.",
        *legacy_entry_points(config),
    ]
    utils.atomic_write(state / "STATUS.md", ("\n".join(lines) + "\n").encode("utf-8"), 0o600)


def generate_capsule(
    config: Dict[str, Any],
    conn: sqlite3.Connection,
    harness: str,
    session_id: str,
    current_capture: str,
) -> None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE harness=? AND session_id=?",
        (harness, session_id),
    ).fetchone()
    if not row:
        return
    tools = {}
    with contextlib.suppress(Exception):
        tools = json.loads(row["tool_counts_json"] or "{}")
    refs = conn.execute(
        "SELECT kind,value FROM references_found WHERE harness=? AND session_id=? ORDER BY kind,value",
        (harness, session_id),
    ).fetchall()
    relative = pathlib.Path("indexes") / "sessions" / harness / f"{session_id}.md"
    destination = configuration.state_root(config) / relative
    lines = [
        f"# {harness.capitalize()} session `{session_id}`",
        "",
        f"Deterministic capsule generator: `agentarchive/{VERSION}`. No model-generated claims are introduced.",
        "",
        f"- Start: `{row['started_utc'] or 'unknown'}`",
        f"- Latest indexed event: `{row['ended_utc'] or 'unknown'}`",
        f"- Working directory: `{row['cwd'] or 'unknown'}`",
        f"- Model/provider: `{row['model'] or 'unknown'}`",
        f"- Parent/root: `{row['parent_id'] or ''}` / `{row['root_id'] or ''}`",
        f"- Archived source: `{row['source_relpath']}` (SHA-256 `{row['source_sha256'] or 'pending'}`)",
        f"- Updated by capture: `{current_capture}`",
        "",
        "## First request",
        "",
        row["first_prompt"] or "_Not present in the post-legacy incremental index; use the historical evidence database._",
        "",
        "## Latest user request",
        "",
        row["latest_user"] or "_None indexed._",
        "",
        "## Latest assistant text",
        "",
        row["latest_assistant"] or "_None indexed._",
        "",
        "## Tool calls",
        "",
    ]
    if tools:
        lines.extend(f"- `{name}`: {count:,}" for name, count in sorted(tools.items(), key=lambda item: (-item[1], item[0])))
    else:
        lines.append("_No post-cutoff tool calls indexed._")
    lines += ["", "## References", ""]
    if refs:
        lines.extend(f"- `{ref['kind']}`: {ref['value']}" for ref in refs)
    else:
        lines.append("_No structured references extracted._")
    lines += [
        "",
        "## Exact evidence",
        "",
        "```bash",
        f"agent-archive search {shlex.quote(session_id)}",
        "```",
        "",
        "Revalidate mutable Git, PR, remote-host, pod, and chain state before acting.",
    ]
    utils.atomic_write(destination, ("\n".join(lines) + "\n").encode("utf-8"), 0o600)
    conn.execute(
        "UPDATE sessions SET capsule_path=? WHERE harness=? AND session_id=?",
        (str(destination), harness, session_id),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO summary_jobs(
            harness,session_id,source_sha256,generator_version,prompt_hash,status,queued_utc,completed_utc
        ) VALUES(?,?,?,?,?,'complete',?,?)
        """,
        (
            harness,
            session_id,
            row["source_sha256"],
            f"deterministic-{VERSION}",
            utils.sha256_text("deterministic capsule: first/latest/tools/references/source coordinates"),
            utils.utc_now(),
            utils.utc_now(),
        ),
    )


def index_legacy_documents(config: Dict[str, Any], conn: sqlite3.Connection) -> None:
    roots = {
        (entry["harness"], pathlib.Path(entry["start_here"]).expanduser().parent)
        for entry in config.get("legacy_indexes", []) if entry.get("start_here")
    }
    for harness, root in sorted(roots):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                raw = path.read_bytes()
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            old = conn.execute("SELECT document_id,sha256 FROM documents WHERE path=?", (str(path),)).fetchone()
            if old and old["sha256"] == digest:
                continue
            title_match = re.search(r"^#\s+(.+)$", text, re.M)
            title = title_match.group(1).strip() if title_match else path.stem
            if old:
                conn.execute("DELETE FROM documents_fts WHERE document_id=?", (old["document_id"],))
                conn.execute(
                    "UPDATE documents SET harness=?,title=?,sha256=?,text=?,indexed_utc=? WHERE document_id=?",
                    (harness, title, digest, text, utils.utc_now(), old["document_id"]),
                )
                document_id = int(old["document_id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO documents(harness,path,title,sha256,text,indexed_utc) VALUES(?,?,?,?,?,?)",
                    (harness, str(path), title, digest, text, utils.utc_now()),
                )
                document_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO documents_fts VALUES(?,?,?,?,?)",
                (text, title, str(path), harness, document_id),
            )
    conn.commit()


def render_start_here(config: Dict[str, Any], conn: sqlite3.Connection) -> None:
    state = configuration.state_root(config)
    cli = shlex.quote(str(pathlib.Path.home() / ".local/bin/agent-archive"))
    latest = catalog.latest_success(conn)
    reasoning_counts = reasoning_index.stats(conn)
    counts = {
        row["harness"]: row["count"]
        for row in conn.execute("SELECT harness,COUNT(*) AS count FROM sessions GROUP BY harness")
    }
    recent = conn.execute(
        "SELECT harness,session_id,ended_utc,cwd,first_prompt,capsule_path FROM sessions ORDER BY ended_utc DESC LIMIT 40"
    ).fetchall()
    lines = [
        "# Agent work archive: start here",
        "",
        "This is the live cross-harness entry point. Its Markdown, session capsules, SQLite catalog, and original live source files are plaintext and directly readable under this macOS account. The encrypted restic snapshots are a separate disaster-recovery layer and are not used for ordinary search or onboarding.",
        "",
        (
            f"Indexed through successful capture `{latest['capture_id']}` finished `{latest['finished_utc']}`."
            if latest else
            "No successful incremental capture is recorded yet."
        ),
        "If the needed work is newer, run a filtered capture or inspect only an already-known non-incognito source coordinate; never recursively search the live transcript roots.",
        "",
        "## Read path",
        "",
        "1. Identify the relevant harness/project below.",
        "2. Read the configured historical workstream and lineage/family indexes for older work.",
        "3. Read only the relevant session capsules.",
        f"4. Use `{cli} search 'QUERY'` for exact evidence across historical and incremental indexes.",
        f"   Use `{cli} reasoning 'QUERY'` when prior hypotheses and rationale are the target.",
        f"5. Use `{cli} serve` for a paged visual transcript with tool inputs/results and readable reasoning.",
        "6. Return a context receipt and revalidate mutable external state before editing.",
        "",
        "## Historical entry points",
        "",
        *(legacy_entry_points(config) or ["No historical indexes configured."]),
        "",
        "## Incremental coverage",
        "",
        f"- Claude sessions indexed: **{counts.get('claude', 0):,}**",
        f"- Codex threads indexed: **{counts.get('codex', 0):,}**",
        f"- Deduplicated readable reasoning payloads: **{reasoning_counts['payloads']:,}** "
        f"from **{reasoning_counts['occurrences']:,}** raw occurrences",
        "- Deterministic capsules contain no inferred success claims; exact transcripts decide.",
        "",
        "## Recent changed sessions",
        "",
        "| Time | Harness | Session | Working directory | First request |",
        "|---|---|---|---|---|",
    ]
    for row in recent:
        capsule = row["capsule_path"] or ""
        label = f"[{row['session_id']}]({capsule})" if capsule else row["session_id"]
        lines.append(
            f"| {row['ended_utc'] or ''} | {row['harness']} | {label} | {utils.md_cell(row['cwd'] or '', 80)} | {utils.md_cell(row['first_prompt'] or '', 160)} |"
        )
    lines += [
        "",
        "## Search",
        "",
        "```bash",
        f"{cli} search 'timeout AND retry'",
        f"{cli} reasoning 'rejected approach'",
        f"{cli} search 'SESSION_UUID'",
        f"{cli} context 'i:codex:BLOCK_ID' --max-chars 4000",
        f"{cli} context 'r:codex:PAYLOAD_ID' --max-chars 4000",
        f"{cli} context 'l:claude:BLOCK_ID' --max-chars 4000",
        f"{cli} context '/absolute/raw/path.jsonl:LINE' --max-chars 4000",
        f"{cli} serve",
        f"{cli} status",
        "```",
        "",
        "Search is a terse routing step (eight deduplicated 18-token hit cards by default). Its text view uses compact session coordinates; selected `context` output and `search --json` retain full provenance. Expand only a selected hit ID or incremental raw coordinate; do not ingest a whole transcript or raise the character budget speculatively.",
        "",
        "Summaries route; raw archived evidence decides. Authentication files and browser credentials are deliberately excluded from capture.",
    ]
    utils.atomic_write(state / "START_HERE.md", ("\n".join(lines) + "\n").encode("utf-8"), 0o600)
