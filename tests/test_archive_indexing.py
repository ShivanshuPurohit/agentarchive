"""Indexing behavior preserved from the original archive test suite."""

import pathlib
import tempfile
import unittest
from unittest import mock
import agent_archive as aa
from agentarchive import common, indexing


class ArchiveIndexingTests(unittest.TestCase):
    def test_unchanged_jsonl_reuses_prior_hash_instead_of_rehashing_rollout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            stage = root / "stage"
            rollout = stage / "data" / "codex" / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"event":"retained"}\n', encoding="utf-8")
            info = rollout.stat()
            conn.execute(
                """
                INSERT INTO latest_files(
                    harness,relpath,file_type,size_bytes,mtime_ns,mode,uid,gid,
                    sha256,link_target,present,first_capture,last_capture
                ) VALUES(?,?,?,?,?,?,?,?,?,?,1,'prior','prior')
                """,
                (
                    "codex", "sessions/rollout.jsonl", "file", info.st_size,
                    info.st_mtime_ns, aa.stat.S_IMODE(info.st_mode), info.st_uid,
                    info.st_gid, "prior-sha", None,
                ),
            )
            conn.commit()
            with mock.patch.object(common, "sha256_file") as hashed:
                rows, counts = aa.inventory_stage(conn, stage, "current")
            hashed.assert_not_called()
            self.assertEqual(rows[0]["sha256"], "prior-sha")
            self.assertEqual(counts["unchanged"], 1)
            conn.close()

    def test_unchanged_rollout_is_not_reparsed_by_incremental_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            stage = root / "stage"
            rollout = stage / "data" / "codex" / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('{"event":"retained"}\n', encoding="utf-8")
            info = rollout.stat()
            conn.execute(
                """
                INSERT INTO indexed_files(
                    harness,relpath,size_bytes,mtime_ns,byte_offset,line_no,
                    prefix_sha256,last_capture,tool_calls_version
                ) VALUES('codex','sessions/rollout.jsonl',?,?,?,?,?,'prior',?)
                """,
                (
                    info.st_size, info.st_mtime_ns, info.st_size, 1,
                    aa.prefix_sha256(rollout), indexing.TOOL_CALLS_VERSION,
                ),
            )
            conn.commit()
            inventory = [{
                "harness": "codex",
                "relpath": "sessions/rollout.jsonl",
                "sha256": "prior-sha",
                "change_kind": "unchanged",
            }]
            with mock.patch.object(indexing, "extract_event") as extracted:
                aa.index_capture(config, conn, stage, "current", inventory)
            extracted.assert_not_called()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], 0)
            conn.close()

    def test_extract_claude_message_and_tool(self):
        obj = {
            "timestamp": "2026-07-20T12:00:00Z",
            "sessionId": "11111111-1111-4111-8111-111111111111",
            "cwd": "/tmp/repo",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "done"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                ],
            },
        }
        sid, _, cwd, model, blocks, meta = aa.extract_event("claude", "projects/x.jsonl", obj)
        self.assertEqual(sid, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(cwd, "/tmp/repo")
        self.assertEqual(model, "claude-test")
        self.assertEqual(meta["timestamp"], "2026-07-20T12:00:00Z")
        self.assertTrue(any(block[2] == "done" for block in blocks))
        self.assertTrue(any(block[0] == "tool_call" and block[1] == "Bash" for block in blocks))

    def test_extract_codex_message(self):
        obj = {
            "timestamp": "2026-07-20T12:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "verified"}],
            },
        }
        sid, _, _, _, blocks, _ = aa.extract_event(
            "codex",
            "sessions/rollout-11111111-1111-4111-8111-111111111111.jsonl",
            obj,
        )
        self.assertEqual(sid, "11111111-1111-4111-8111-111111111111")
        self.assertTrue(any(block[2] == "verified" and block[3] == "assistant" for block in blocks))

    def test_inventory_records_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            conn.execute(
                """
                INSERT INTO latest_files(harness,relpath,file_type,size_bytes,mtime_ns,mode,uid,gid,
                                         sha256,link_target,present,first_capture,last_capture)
                VALUES('claude','gone.txt','file',4,1,384,0,0,'abc',NULL,1,'old','old')
                """
            )
            conn.commit()
            stage = root / "stage"
            (stage / "data" / "claude").mkdir(parents=True)
            path = stage / "data" / "claude" / "new.txt"
            path.write_text("new", encoding="utf-8")
            rows, counts = aa.inventory_stage(conn, stage, "newcapture")
            self.assertEqual(counts["new"], 1)
            self.assertEqual(counts["deleted"], 1)
            self.assertEqual({row["change_kind"] for row in rows}, {"new", "deleted"})
            conn.close()
