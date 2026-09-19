import io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import agent_archive as aa
from privacy_filter import transcript_is_excluded, workspace_is_excluded
from transcript_viewer import Registry


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class WorkspaceExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.private = self.root / "private"
        self.config = aa.default_config(self.root / "state")
        self.config["excluded_workspaces"] = [str(self.private)]
        self.config["legacy_indexes"] = []
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.config["sources"]["claude"].update(
            root=str(self.claude), paths=["projects"], sqlite=[]
        )
        self.config["sources"]["codex"].update(
            root=str(self.codex), paths=["sessions"], sqlite=[]
        )
        aa.ensure_private_tree(self.root / "state")
        aa.connect_catalog(self.config).close()

    def tearDown(self):
        self.temp.cleanup()

    def fixtures(self):
        ids = {
            "private_claude": "11111111-1111-4111-8111-111111111111",
            "public_claude": "22222222-2222-4222-8222-222222222222",
            "private_codex": "33333333-3333-4333-8333-333333333333",
            "public_codex": "44444444-4444-4444-8444-444444444444",
        }
        paths = {
            "private_claude": self.claude / "projects/private" / f"{ids['private_claude']}.jsonl",
            "public_claude": self.claude / "projects/public" / f"{ids['public_claude']}.jsonl",
            "private_codex": self.codex / "sessions/2026/08/17" / f"rollout-{ids['private_codex']}.jsonl",
            "public_codex": self.codex / "sessions/2026/08/17" / f"rollout-{ids['public_codex']}.jsonl",
        }
        for name in ("private_claude", "public_claude"):
            cwd = self.private / "nested" if name.startswith("private") else self.root / "public"
            write_jsonl(paths[name], [{"type": "user", "sessionId": ids[name], "cwd": str(cwd),
                                      "message": {"content": name}}])
        for name in ("private_codex", "public_codex"):
            cwd = self.private / "nested" if name.startswith("private") else self.root / "public"
            write_jsonl(paths[name], [{"type": "session_meta", "payload": {
                "id": ids[name], "session_id": ids[name], "cwd": str(cwd)}}])
        return ids, paths

    def test_capture_search_and_viewer_enforce_both_harnesses(self):
        ids, paths = self.fixtures()
        self.assertTrue(workspace_is_excluded(self.config, str(self.private / "child")))
        self.assertTrue(transcript_is_excluded(self.config, paths["private_claude"]))
        self.assertTrue(transcript_is_excluded(self.config, paths["private_codex"]))
        stage = self.root / "stage"
        warnings = aa.stage_sources(self.config, stage)
        staged = {path.name for path in (stage / "data").rglob("*.jsonl")}
        self.assertNotIn(paths["private_claude"].name, staged)
        self.assertNotIn(paths["private_codex"].name, staged)
        self.assertIn(paths["public_claude"].name, staged)
        self.assertIn(paths["public_codex"].name, staged)
        self.assertTrue(any("claude=1" in item and "codex=1" in item for item in warnings))
        conn = aa.connect_catalog(self.config)
        try:
            aa.index_capture(self.config, conn, stage, "viewer-inventory", [])
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(aa.search_archive(self.config, ids["private_codex"]), [])
        owners = {source.owner for source in Registry(self.config).sources.values()}
        self.assertNotIn(ids["private_claude"], owners)
        self.assertNotIn(ids["private_codex"], owners)
        self.assertIn(ids["public_claude"], owners)
        self.assertIn(ids["public_codex"], owners)

    def test_claude_hook_refuses_private_checkpoint_and_queue(self):
        ids, paths = self.fixtures()
        payload = {"session_id": ids["private_claude"],
                   "transcript_path": str(paths["private_claude"]),
                   "cwd": str(self.private / "nested"), "reason": "SessionEnd"}
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        with mock.patch.object(aa.sys, "stdin", stdin):
            result = aa.enqueue_hook(self.config)
        self.assertEqual(result["status"], "excluded_workspace")
        self.assertFalse(any((self.root / "state" / "queue").iterdir()))
        self.assertFalse((self.root / "state" / "inbox").exists())

    def test_tool_call_refresh_does_not_reintroduce_excluded_transcripts(self):
        _, paths = self.fixtures()
        for name in ("private_codex", "public_codex"):
            with paths[name].open("a") as handle:
                handle.write(json.dumps({"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "exec", "input": name + "commandneedle",
                }}) + "\n")
        stage = self.root / "stage"
        aa.stage_sources(self.config, stage)
        conn = aa.connect_catalog(self.config)
        try:
            aa.index_capture(self.config, conn, stage, "refresh", [])
            conn.execute("UPDATE indexed_files SET tool_calls_version=0")
            conn.commit()
            aa.index_capture(self.config, conn, stage, "backfill", [])
        finally:
            conn.close()
        self.assertEqual(aa.search_archive(self.config, "private_codexcommandneedle"), [])
        self.assertEqual(len(aa.search_archive(self.config, "public_codexcommandneedle")), 1)

    def test_new_exclusion_purges_prior_derived_content(self):
        conn = aa.connect_catalog(self.config)
        capsule = self.root / "state/indexes/sessions/codex/private.md"
        capsule.parent.mkdir(parents=True, exist_ok=True)
        capsule.write_text("private", encoding="utf-8")
        key = ("codex", "private-session")
        conn.execute(
            "INSERT INTO sessions(harness,session_id,cwd,source_relpath,updated_capture,capsule_path) "
            "VALUES(?,?,?,?,?,?)", (*key, str(self.private), "sessions/private.jsonl", "capture", str(capsule))
        )
        cursor = conn.execute(
            "INSERT INTO blocks(harness,session_id,source_relpath,line_no,block_index,text,text_sha256,capture_id) "
            "VALUES(?,?,?,?,?,?,?,?)", (*key, "sessions/private.jsonl", 1, 0, "private", "hash", "capture")
        )
        conn.execute("INSERT INTO blocks_fts VALUES(?,?,?,?,?,?,?,?,?)",
                     ("private", *key, "sessions/private.jsonl", 1, cursor.lastrowid, "user", "text", ""))
        payload = conn.execute(
            "INSERT INTO reasoning_payloads(text_sha256,text) VALUES('reason-hash','private reason')"
        ).lastrowid
        conn.execute(
            "INSERT INTO reasoning_occurrences(payload_id,harness,session_id,source_relpath,line_no,kind,capture_id) "
            "VALUES(?,?,?,?,?,?,?)", (payload, *key, "sessions/private.jsonl", 2, "agent_reasoning", "capture")
        )
        conn.commit()
        self.assertEqual(aa.purge_excluded_sessions(self.config, conn), 1)
        for table in ("sessions", "blocks", "blocks_fts", "reasoning_payloads", "reasoning_fts"):
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        conn.close()
        self.assertFalse(capsule.exists())


if __name__ == "__main__":
    unittest.main()
