"""Compact redundant derived state without deleting rollout or snapshot history."""

from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import sqlite3
from typing import Any, Dict

from . import catalog, common as utils, config as configuration


INVENTORY_FIELDS = (
    "harness", "relpath", "file_type", "size_bytes", "mtime_ns", "mode", "uid", "gid",
    "sha256", "link_target", "change_kind",
)


def evidence_fingerprint(conn: sqlite3.Connection) -> Dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute("SELECT block_id,text_sha256 FROM blocks ORDER BY block_id"):
        digest.update(f"{row[0]}:{row[1]}\n".encode())
        count += 1
    return {"blocks": count, "identity_sha256": digest.hexdigest()}


def compact_catalog(config: Dict[str, Any], apply: bool = False) -> Dict[str, Any]:
    """Require exact manifest replacement and preserve every block ID/hash."""
    path = catalog.catalog_path(config)
    conn = catalog.connect_catalog_readonly(config)
    eligible, retained = [], []
    redundant = 0
    try:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='blocks_fts'").fetchone()[0]
        external = "content='blocks'" in schema.replace(" ", "").replace('"', "'")
        before = evidence_fingerprint(conn)
        captures = conn.execute(
            "SELECT f.capture_id,count(*) AS rows,c.manifest_sha256 FROM files f "
            "JOIN captures c USING(capture_id) WHERE f.change_kind='unchanged' GROUP BY f.capture_id"
        ).fetchall()
        for index, capture in enumerate(captures):
            manifest = configuration.state_root(config) / "manifests" / f"{capture['capture_id']}.jsonl.gz"
            if not manifest.is_file() or utils.sha256_file(manifest) != capture["manifest_sha256"]:
                retained.append(capture["capture_id"])
                continue
            with gzip.open(manifest, "rt", encoding="utf-8") as handle:
                full = [json.loads(line) for line in handle]
            expected = {tuple(row.get(field) for field in INVENTORY_FIELDS)
                        for row in full if row.get("change_kind") == "unchanged"}
            actual = {tuple(row[field] for field in INVENTORY_FIELDS) for row in conn.execute(
                "SELECT * FROM files WHERE capture_id=? AND change_kind='unchanged'", (capture["capture_id"],),
            )}
            if expected != actual or len(expected) != capture["rows"]:
                retained.append(capture["capture_id"])
                continue
            eligible.append(capture["capture_id"])
            redundant += capture["rows"]
            if index % 25 == 0:
                utils.progress(f"compact: checked {index + 1}/{len(captures)} capture manifests")
        result = {"applied": False, "before_bytes": path.stat().st_size,
                  "evidence": before, "external_content_index": external,
                  "redundant_inventory_rows": redundant, "retained_unverified_captures": retained}
        if not apply:
            return result
        if not external:
            mismatch = conn.execute(
                "SELECT count(*) FROM blocks_fts f LEFT JOIN blocks b ON b.block_id=f.block_id "
                "WHERE b.block_id IS NULL OR b.text != f.text"
            ).fetchone()[0]
            if mismatch:
                raise RuntimeError(f"refusing to replace FTS: {mismatch} rows differ from source blocks")
    finally:
        conn.close()
    rollback = path.with_name("catalog.compact-rollback.sqlite")
    if rollback.exists():
        raise RuntimeError(f"a previous compaction rollback still exists: {rollback}")
    conn = catalog.connect_catalog(config)
    try:
        utils.progress("compact: saving consistent rollback catalog")
        backup = sqlite3.connect(str(rollback))
        try:
            conn.backup(backup)
        finally:
            backup.close()
        rollback.chmod(0o600)
        with conn:
            if not external:
                utils.progress("compact: rebuilding search over existing text, without a second text copy")
                conn.execute("DROP TABLE blocks_fts")
                conn.execute(catalog.BLOCKS_FTS_SQL)
                conn.execute("INSERT INTO blocks_fts(blocks_fts) VALUES('rebuild')")
            conn.executemany("DELETE FROM files WHERE capture_id=? AND change_kind='unchanged'",
                             [(capture_id,) for capture_id in eligible])
            conn.execute("INSERT INTO blocks_fts(blocks_fts,rank) VALUES('integrity-check',1)")
            if evidence_fingerprint(conn) != before:
                raise RuntimeError("compaction changed evidence identities; transaction rolled back")
        utils.progress("compact: reclaiming unused database pages")
        conn.execute("VACUUM")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"compacted catalog check failed; rollback retained at {rollback}")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result.update(applied=True, external_content_index=True, after_bytes=path.stat().st_size)
        result["reclaimed_bytes"] = result["before_bytes"] - result["after_bytes"]
        receipt = configuration.state_root(config) / "audits" / f"compact-{utils.capture_id()}.json"
        utils.write_json(receipt, result)
        result["receipt"] = str(receipt)
        rollback.unlink()
        return result
    finally:
        conn.close()
