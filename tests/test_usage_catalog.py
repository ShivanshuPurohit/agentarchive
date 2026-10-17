"""Model-call receipts index once per call into `usage`, backfill once, and leave with their session."""

import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import agent_archive as aa
from agentarchive import receipts

CLAUDE = "77777777-7777-4777-8777-777777777777"
CODEX = "88888888-8888-4888-8888-888888888888"
RECEIPT = {"input_tokens": 10, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 30}


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def reply(message_id, model, text, usage, at, cwd):
    return {"type": "assistant", "sessionId": CLAUDE, "cwd": cwd, "timestamp": at,
            "message": {"id": message_id, "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": text}], "usage": usage}}


def claude_rows(cwd):
    """A prompt, one reply written as two content-block lines, a synthetic line, and a second call.

    The prompt outgrows the 64 KB prefix the indexer hashes, so a later append resumes the scan.
    """
    return [
        {"type": "user", "sessionId": CLAUDE, "cwd": cwd, "timestamp": "2026-10-15T10:00:00Z",
         "message": {"role": "user", "content": "Count the beans. " * 4000}},
        reply("msg_1", "claude-fable-5-1", "Counting", RECEIPT, "2026-10-15T10:00:05Z", cwd),
        reply("msg_1", "claude-fable-5-1", "Done", RECEIPT, "2026-10-15T10:00:06Z", cwd),
        reply("msg_2", "<synthetic>", "Interrupted", RECEIPT, "2026-10-15T10:00:07Z", cwd),
        reply("msg_3", "claude-opus-5-5", "A second call",
              {"input_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 7},
              "2026-10-15T10:01:00Z", cwd),
    ]


def codex_rows(cwd):
    """One response, its aggregate mirrored once more as rollouts do."""
    total = {"input_tokens": 50, "cached_input_tokens": 40, "output_tokens": 9, "reasoning_output_tokens": 4, "total_tokens": 59}
    count = {"type": "event_msg", "timestamp": "2026-10-15T11:00:00Z",
             "payload": {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": dict(total)}}}
    return [
        {"type": "session_meta", "timestamp": "2026-10-15T10:59:00Z",
         "payload": {"id": CODEX, "cwd": cwd, "timestamp": "2026-10-15T10:59:00Z"}},
        {"type": "turn_context", "timestamp": "2026-10-15T10:59:01Z", "payload": {"cwd": cwd, "model": "gpt-5.6-sol"}},
        {"type": "response_item", "timestamp": "2026-10-15T10:59:30Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Count the beans"}]}},
        count,
        {**count, "timestamp": "2026-10-15T11:00:01Z"},
    ]


class UsageCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.cwd = str(self.root / "work")
        self.config = aa.default_config(self.root / "state")
        self.config["legacy_indexes"] = []
        self.config["excluded_workspaces"] = [str(self.root / "private")]
        for harness, definition in self.config["sources"].items():
            definition.update(root=str(self.root / harness), paths=[definition["paths"][0]], sqlite=[])
        aa.ensure_private_tree(self.root / "state")
        aa.connect_catalog(self.config).close()
        self.claude = self.root / "claude" / "projects" / "-work" / f"{CLAUDE}.jsonl"
        self.codex = self.root / "codex" / "sessions" / "2026" / "10" / "15" / f"rollout-{CODEX}.jsonl"
        write_jsonl(self.claude, claude_rows(self.cwd))
        write_jsonl(self.claude.with_name(self.claude.name + ".pre-import"), claude_rows(self.cwd))
        write_jsonl(self.codex, codex_rows(self.cwd))

    def tearDown(self):
        self.temp.cleanup()

    def index(self, capture):
        stage = self.root / f"stage-{capture}"
        aa.stage_sources(self.config, stage)
        conn = aa.connect_catalog(self.config)
        stats = aa.index_capture(self.config, conn, stage, capture, [])
        conn.commit()
        return conn, stats

    @staticmethod
    def rows(conn):
        return [tuple(row) for row in conn.execute(
            "SELECT harness, identity, model, input_tokens, cached_tokens, cache_write_tokens, output_tokens, "
            "reasoning_tokens, source_relpath FROM usage ORDER BY harness, line_no"
        )]

    def test_each_model_call_is_one_row_read_from_the_canonical_file(self):
        conn, stats = self.index("capture-1")
        try:
            claude_rel = self.claude.relative_to(self.root / "claude").as_posix()
            codex_rel = self.codex.relative_to(self.root / "codex").as_posix()
            self.assertEqual(self.rows(conn), [
                ("claude", "message:msg_1", "claude-fable-5-1", 130, 100, 20, 30, None, claude_rel),
                ("claude", "message:msg_3", "claude-opus-5-5", 5, 0, 0, 7, None, claude_rel),
                ("codex", "usage:4", "gpt-5.6-sol", 50, 40, None, 9, 4, codex_rel),
            ])
            self.assertEqual(stats["model_calls_added"], 3)
            self.assertEqual({row[0] for row in conn.execute("SELECT usage_version FROM indexed_files")}, {1})
        finally:
            conn.close()

    def test_a_version_bump_backfills_once_and_a_resumed_scan_adds_only_the_new_call(self):
        conn, _ = self.index("capture-1")
        blocks = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        conn.execute("DELETE FROM usage")
        conn.execute("UPDATE indexed_files SET usage_version=0")
        conn.commit()
        conn.close()
        conn, stats = self.index("capture-2")
        try:
            self.assertEqual(stats["files_backfilled"], 3)
            self.assertEqual(len(self.rows(conn)), 3)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0], blocks)
            self.assertEqual({row[0] for row in conn.execute("SELECT usage_version FROM indexed_files")}, {1})
        finally:
            conn.close()
        with self.claude.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(reply(
                "msg_4", "claude-fable-5-1", "One more",
                {"input_tokens": 1, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 3, "output_tokens": 4},
                "2026-10-15T10:02:00Z", self.cwd,
            )) + "\n")
        conn, stats = self.index("capture-3")
        try:
            self.assertEqual(stats["files_backfilled"], 0)
            self.assertEqual(stats["model_calls_added"], 1)
            self.assertEqual([row[1] for row in self.rows(conn)],
                             ["message:msg_1", "message:msg_3", "message:msg_4", "usage:4"])
        finally:
            conn.close()

    def test_a_deleted_transcript_gets_its_receipts_back_from_the_snapshot_still_holding_it(self):
        conn, _ = self.index("capture-1")
        stage = self.root / "stage-capture-1"
        claude_rel = self.claude.relative_to(self.root / "claude").as_posix()
        codex_rel = self.codex.relative_to(self.root / "codex").as_posix()
        # Both transcripts were scanned before receipts existed and left the disk; the Claude one was
        # last captured into a snapshot the repository keeps, the Codex one only into a forgotten one.
        conn.execute("DELETE FROM usage")
        conn.execute("UPDATE indexed_files SET usage_version=0 WHERE relpath IN (?, ?)", (claude_rel, codex_rel))
        conn.executemany(
            "INSERT INTO captures(capture_id,started_utc,reason,status,restic_snapshot) VALUES(?,?,?,?,?)",
            [("capture-0", "2026-10-14T00:00:00Z", "manual", "complete", "forgotten"),
             ("capture-1", "2026-10-15T00:00:00Z", "manual", "complete", "kept")],
        )
        conn.executemany(
            "INSERT INTO files(capture_id,harness,relpath,file_type,size_bytes,sha256,change_kind) VALUES(?,?,?,?,?,?,?)",
            [("capture-0", "claude", claude_rel, "file", 1, "a", "new"),
             ("capture-1", "claude", claude_rel, "file", 2, "b", "changed"),
             ("capture-0", "codex", codex_rel, "file", 3, "c", "new")],
        )
        conn.commit()
        restores = []

        def restore(config, snapshot, paths, target):
            restores.append((snapshot, paths))
            for path in paths:
                copy = target / path.lstrip("/")
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(stage / path.lstrip("/"), copy)

        try:
            with mock.patch.object(receipts, "live_snapshots", return_value={"capture-1": "kept"}), \
                    mock.patch.object(receipts, "restore", side_effect=restore):
                dry = receipts.backfill(self.config, conn, dry_run=True)
                self.assertEqual((dry["restorable"], dry["unrecoverable"], dry["snapshots"], dry["bytes"]), (1, 1, 1, 2))
                self.assertEqual(self.rows(conn), [])
                result = receipts.backfill(self.config, conn)
            self.assertEqual(restores, [("kept", [f"/data/claude/{claude_rel}"])])
            self.assertEqual((result["files_backfilled"], result["model_calls_added"], result["unrecoverable"]), (1, 2, 1))
            self.assertEqual([row[1] for row in self.rows(conn)], ["message:msg_1", "message:msg_3"])
            self.assertEqual(
                dict(conn.execute("SELECT relpath, usage_version FROM indexed_files WHERE relpath IN (?, ?)", (claude_rel, codex_rel))),
                {claude_rel: 1, codex_rel: 0},
            )
            self.assertEqual(sorted(path.name for path in (self.root / "state").iterdir() if path.name.startswith("receipts-")), [])
        finally:
            conn.close()

    def test_purging_an_excluded_workspace_drops_its_receipts(self):
        conn, _ = self.index("capture-1")
        try:
            self.config["excluded_workspaces"].append(self.cwd)
            self.assertEqual(aa.purge_excluded_sessions(self.config, conn), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
