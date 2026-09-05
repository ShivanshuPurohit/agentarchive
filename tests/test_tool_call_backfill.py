"""Retained commands must survive extractor upgrades, failures, and later appends."""

import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

import agent_archive as aa
from agentarchive import indexing


SID = "12345678-1234-4234-8234-123456789abc"


def record(kind, **fields):
    return {"type": "response_item", "timestamp": "2026-08-24T12:00:00Z",
            "payload": {"type": kind, **fields}}


class ToolCallBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.stage = self.root / "stage"
        self.config = aa.default_config(self.root / "state")
        self.config["legacy_indexes"] = []
        for harness in self.config["sources"]:
            self.config["sources"][harness]["root"] = str(self.stage / "data" / harness)
        aa.ensure_private_tree(self.root / "state")
        self.conn = aa.connect_catalog(self.config)
        self.addCleanup(self.conn.close)
        self.path = self.stage / "data/codex/sessions" / f"rollout-{SID}.jsonl"
        self.path.parent.mkdir(parents=True)
        rows = [
            {"type": "session_meta", "timestamp": "2026-08-24T12:00:00Z",
             "payload": {"id": SID, "cwd": str(self.root / "public"), "padding": "x" * 70000}},
            record("message", role="user", content=[{"type": "input_text", "text": "collect evidence"}]),
            record("function_call", name="shell", call_id="old", arguments='{"cmd":"oldercommand"}'),
            record("function_call_output", call_id="old", output="retained old output"),
            record("custom_tool_call", name="exec", call_id="new",
                   input='await tools.exec_command({cmd: "newcommandneedle"});'),
            record("custom_tool_call_output", call_id="new", output="retained new output"),
        ]
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def index(self, capture="refresh"):
        return aa.index_capture(self.config, self.conn, self.stage, capture, [])

    def legacy_index(self):
        extract = indexing.extract_event

        def previous_extractor(harness, relative, obj):
            if (obj.get("payload") or {}).get("type") == "custom_tool_call":
                obj = {**obj, "payload": {k: v for k, v in obj["payload"].items() if k != "input"}}
            return extract(harness, relative, obj)

        with mock.patch.object(indexing, "extract_event", side_effect=previous_extractor):
            self.index("old-capture")
        self.conn.execute("UPDATE indexed_files SET tool_calls_version=0")
        self.conn.commit()

    def test_unchanged_file_backfill_retains_old_ids_outputs_and_raw_bytes(self):
        self.legacy_index()
        before = self.path.read_bytes()
        old = {row["block_id"]: dict(row) for row in self.conn.execute("SELECT * FROM blocks")}
        self.assertEqual(aa.search_archive(self.config, "newcommandneedle"), [])
        result = self.index()
        self.assertEqual(result["files_backfilled"], 1)
        self.assertEqual(result["tool_calls_added"], 1)
        self.assertEqual(self.path.read_bytes(), before)
        for block_id, row in old.items():
            self.assertEqual(dict(self.conn.execute("SELECT * FROM blocks WHERE block_id=?", (block_id,)).fetchone()), row)
        hits = aa.search_archive(self.config, "newcommandneedle")
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["kind"], hits[0]["tool_name"]), ("tool_call", "exec"))
        self.assertIn('cmd: "newcommandneedle"', aa.indexed_context(self.config, hits[0]["hit_id"]))
        session = self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (SID,)).fetchone()
        self.assertEqual(json.loads(session["tool_counts_json"]), {"shell": 1, "exec": 1})
        self.assertIn("`exec`: 1", pathlib.Path(session["capsule_path"]).read_text())
        count = self.conn.execute("SELECT count(*) FROM blocks").fetchone()[0]
        with mock.patch.object(indexing, "extract_event", side_effect=AssertionError("rescanned")):
            repeated = self.index("repeat")
        self.assertEqual(repeated, {"files_scanned": 0, "files_backfilled": 0, "tool_calls_added": 0})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blocks").fetchone()[0], count)

    def test_failed_backfill_rolls_back_evidence_and_checkpoint_then_retries(self):
        self.legacy_index()
        count = self.conn.execute("SELECT count(*) FROM blocks").fetchone()[0]
        with mock.patch.object(indexing, "insert_reference", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.index("failed")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM blocks").fetchone()[0], count)
        self.assertEqual(self.conn.execute("SELECT tool_calls_version FROM indexed_files").fetchone()[0], 0)
        self.assertEqual(aa.search_archive(self.config, "newcommandneedle"), [])
        self.assertEqual(self.index("retry")["tool_calls_added"], 1)

    def test_partial_tail_and_append_after_backfill_are_indexed_once(self):
        self.legacy_index()
        partial = json.dumps(record("custom_tool_call", name="exec", call_id="tail", input="tailcommandneedle"))
        with self.path.open("a") as handle:
            handle.write(partial)
        self.assertEqual(self.index()["tool_calls_added"], 1)
        self.assertEqual(aa.search_archive(self.config, "tailcommandneedle"), [])
        with self.path.open("a") as handle:
            handle.write("\n")
        self.assertEqual(self.index("complete-tail")["tool_calls_added"], 1)
        self.assertEqual(len(aa.search_archive(self.config, "tailcommandneedle")), 1)
        self.assertEqual(self.index("repeat")["tool_calls_added"], 0)

    def test_claude_calls_and_pre_cutoff_flat_codex_calls_are_searchable(self):
        for definition in self.config["sources"].values():
            definition["legacy_cutoff_utc"] = "2026-07-01T00:00:00Z"
        self.path.write_text(json.dumps({"type": "function_call", "name": "shell",
                                        "arguments": '{"cmd":"flatcommandneedle"}'}) + "\n")
        claude = self.stage / "data/claude/projects/example" / f"{SID}.jsonl"
        claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({
            "sessionId": SID, "timestamp": "2026-01-01T12:00:00Z", "cwd": str(self.root / "public"),
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "historical prose stays in the historical index"},
                {"type": "tool_use", "name": "Bash", "id": "claude-call",
                 "input": {"command": "claudecommandneedle"}},
            ]},
        }) + "\n")
        self.assertEqual(self.index()["tool_calls_added"], 2)
        for query, harness, line_index in (("flatcommandneedle", "codex", 0), ("claudecommandneedle", "claude", 1)):
            hit = aa.search_archive(self.config, query)[0]
            self.assertEqual(hit["harness"], harness)
            self.assertIn(f":1#{line_index}", aa.indexed_context(self.config, hit["hit_id"]))
        # An old checkpoint must not duplicate an already indexed Claude call.
        self.conn.execute("UPDATE indexed_files SET tool_calls_version=0")
        self.conn.commit()
        self.assertEqual(self.index("upgrade")["tool_calls_added"], 0)

    def test_schema_upgrade_marks_old_checkpoints_without_replacing_them(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE indexed_files(
                harness TEXT, relpath TEXT, size_bytes INTEGER, mtime_ns INTEGER,
                byte_offset INTEGER, line_no INTEGER, prefix_sha256 TEXT,
                last_capture TEXT, content_sha256 TEXT, PRIMARY KEY(harness,relpath));
            INSERT INTO indexed_files VALUES('codex','old.jsonl',100,200,100,3,'hash','old','retained-extra');
        """)
        aa.make_schema(conn)
        row = conn.execute("SELECT * FROM indexed_files").fetchone()
        self.assertEqual(row, ("codex", "old.jsonl", 100, 200, 100, 3, "hash", "old", "retained-extra", 0))
        aa.make_schema(conn)
        self.assertEqual(conn.execute("SELECT * FROM indexed_files").fetchone(), row)
