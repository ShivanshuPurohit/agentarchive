"""Unit tests for agent_browse extraction + indexing.

Covers the Claude transcript shape (thinking/text/tool_use/tool_result/attachment/
compaction + parent-prompt linking) and both Codex formats (flat and envelope),
plus write idempotency, FTS population, and resume line-continuation.
"""

import json
import pathlib
import tempfile
import unittest

import agent_browse as ab

SID = "01234567-89ab-cdef-0123-456789abcdef"


def write_jsonl(path: pathlib.Path, records) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")


def claude_record(rtype, uid, parent, content=None, **extra):
    rec = {
        "type": rtype, "uuid": uid, "parentUuid": parent, "sessionId": SID,
        "cwd": "/Users/example/proj", "gitBranch": "main", "version": "2.1.219",
        "userType": "external", "entrypoint": "claude-desktop", "isSidechain": False,
        "timestamp": "2026-08-12T10:00:00.000Z",
        "message": {"role": "assistant" if rtype == "assistant" else "user", "content": content},
    }
    rec.update(extra)
    return rec


def make_claude_file(tmp: pathlib.Path):
    relpath = f"projects/-Users-example-proj/{SID}.jsonl"
    path = tmp / "claude" / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        claude_record("user", "u1", None, "How do I parse Fernet tokens?"),
        claude_record("assistant", "a1", "u1",
                      [{"type": "thinking", "thinking": "Fernet tokens start with gAAAAAB.",
                        "signature": "sig-token"}],
                      **{"message": {"role": "assistant", "model": "claude-sonnet-5",
                                     "content": [{"type": "thinking",
                                                  "thinking": "Fernet tokens start with gAAAAAB.",
                                                  "signature": "sig-token"}]}}),
        claude_record("assistant", "a2", "a1",
                      [{"type": "text", "text": "Here is the answer."}],
                      **{"message": {"role": "assistant", "model": "claude-sonnet-5",
                                     "content": [{"type": "text", "text": "Here is the answer."}]}}),
        claude_record("assistant", "a3", "a2",
                      [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                        "input": {"command": "ls"}}],
                      **{"message": {"role": "assistant", "model": "claude-sonnet-5",
                                     "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                                                  "input": {"command": "ls"}}]}}),
        claude_record("user", "u4", "a3",
                      [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"}],
                      **{"sourceToolAssistantUUID": "a3"}),
        {"type": "attachment", "attachment": {"type": "file", "text": "injected context"},
         "sessionId": SID, "timestamp": "2026-08-12T10:00:01.000Z"},
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
         "logicalParentUuid": "a3", "compactMetadata": {"trigger": "manual"},
         "sessionId": SID, "timestamp": "2026-08-12T10:00:02.000Z"},
    ]
    write_jsonl(path, records)
    return path, relpath


def codex_envelope_records():
    return [
        {"type": "session_meta", "timestamp": "2025-11-01T00:00:00.000Z",
         "payload": {"id": SID, "cwd": "/tmp", "git": {"branch": "main"},
                     "originator": "codex_cli_rs", "cli_version": "0.1.0"}},
        {"type": "turn_context", "timestamp": "2025-11-01T00:00:01.000Z",
         "payload": {"type": "turn_context", "model": "gpt-5.5", "cwd": "/tmp"}},
        {"type": "response_item", "timestamp": "2025-11-01T00:00:02.000Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hello world"}]}},
        {"type": "response_item", "timestamp": "2025-11-01T00:00:03.000Z",
         "payload": {"type": "reasoning", "id": "rs_1",
                     "summary": [{"type": "summary_text", "text": "Considering options"}],
                     "content": None, "encrypted_content": "gAAAAABxxxxxxxxxx"}},
        {"type": "response_item", "timestamp": "2025-11-01T00:00:04.000Z",
         "payload": {"type": "reasoning", "id": "rs_2", "summary": [], "content": None,
                     "encrypted_content": "gAAAAAByyyyyyyyyy"}},
        {"type": "response_item", "timestamp": "2025-11-01T00:00:05.000Z",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"command": ["ls"]}), "call_id": "call_1"}},
        {"type": "response_item", "timestamp": "2025-11-01T00:00:06.000Z",
         "payload": {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"}},
        {"type": "event_msg", "timestamp": "2025-11-01T00:00:07.000Z",
         "payload": {"type": "agent_reasoning", "text": "Let me check the files."}},
        {"type": "event_msg", "timestamp": "2025-11-01T00:00:08.000Z",
         "payload": {"type": "exec_command_end", "call_id": "call_1", "command": ["ls"],
                     "cwd": "/tmp", "stdout": "file.txt", "stderr": "", "exit_code": 0,
                     "aggregated_output": "file.txt", "duration": {"secs": 1, "nanos": 0}}},
        {"type": "event_msg", "timestamp": "2025-11-01T00:00:09.000Z",
         "payload": {"type": "token_count",
                     "info": {"last_token_usage": {"reasoning_output_tokens": 1234}}}},
    ]


def codex_flat_records():
    return [
        {"id": SID, "timestamp": "2025-09-10T00:00:00.000Z", "instructions": None,
         "git": {"branch": "main"}},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "flat reasoning"}],
         "encrypted_content": "gAAAAABzzz"},
        {"record_type": "state"},
        {"type": "function_call", "name": "shell",
         "arguments": json.dumps({"command": ["pwd"]}), "call_id": "call_2"},
        {"type": "function_call_output", "call_id": "call_2", "output": "/tmp"},
    ]


class ClaudeExtractionTests(unittest.TestCase):
    def test_extracts_thinking_text_tool_pairs_and_parent_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, relpath = make_claude_file(pathlib.Path(tmp))
            session, items = ab.extract_claude(path, relpath)
            self.assertEqual(session["session_id"], SID)
            self.assertEqual(session["kind"], "main")
            self.assertEqual(session["model"], "claude-sonnet-5")

            kinds = [it["kind"] for it in items]
            self.assertIn("thinking", kinds)
            self.assertIn("text", kinds)
            self.assertIn("tool_use", kinds)
            self.assertIn("tool_result", kinds)
            self.assertIn("attachment", kinds)
            self.assertIn("compact_summary", kinds)

            thinking = next(it for it in items if it["kind"] == "thinking")
            self.assertIn("gAAAAAB", thinking["content"])
            self.assertEqual(thinking["meta"]["signature_len"], len("sig-token"))
            self.assertIn("parse Fernet", thinking["meta"]["parent_prompt_snippet"])
            self.assertEqual(thinking["meta"]["parent_user_uuid"], "u1")

            tu = next(it for it in items if it["kind"] == "tool_use")
            self.assertEqual(tu["call_id"], "toolu_1")
            self.assertEqual(tu["tool_name"], "Bash")
            self.assertTrue(tu["display_title"].startswith("Bash:"))

            tr = next(it for it in items if it["kind"] == "tool_result")
            self.assertEqual(tr["call_id"], "toolu_1")
            self.assertEqual(tr["content"], "file.txt")


class CodexExtractionTests(unittest.TestCase):
    def test_envelope_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            relpath = f"sessions/2025/11/01/rollout-2025-11-01T00-00-00-{SID}.jsonl"
            path = pathlib.Path(tmp) / "codex" / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(path, codex_envelope_records())
            state, items, offset, line_no = ab.extract_codex(path, relpath)

            self.assertEqual(state.session_id, SID)
            self.assertEqual(state.model, "gpt-5.5")
            self.assertTrue(state.has_encrypted)
            self.assertEqual(state.max_reasoning_tokens, 1234)
            self.assertEqual(line_no, len(codex_envelope_records()))

            kinds = [it["kind"] for it in items]
            self.assertIn("message", kinds)
            self.assertIn("reasoning_summary", kinds)
            self.assertIn("reasoning_encrypted", kinds)
            self.assertIn("function_call", kinds)
            self.assertIn("function_call_output", kinds)
            self.assertIn("agent_reasoning", kinds)
            self.assertIn("exec_end", kinds)

            msg = next(it for it in items if it["kind"] == "message")
            self.assertEqual(msg["content"], "hello world")
            self.assertEqual(msg["role"], "user")

            rsum = next(it for it in items if it["kind"] == "reasoning_summary")
            self.assertEqual(rsum["content"], "Considering options")
            self.assertTrue(rsum["meta"]["encrypted_len"] > 0)

            renc = next(it for it in items if it["kind"] == "reasoning_encrypted")
            self.assertEqual(renc["content"], "")
            self.assertTrue(renc["meta"]["encrypted_len"] > 0)

            fc = next(it for it in items if it["kind"] == "function_call")
            self.assertEqual(fc["call_id"], "call_1")
            self.assertEqual(fc["tool_name"], "shell")

            ex = next(it for it in items if it["kind"] == "exec_end")
            self.assertEqual(ex["exit_code"], 0)
            self.assertEqual(ex["content"], "file.txt")

            # ordering: every item carries an increasing absolute line_no
            seqs = [it["seq"] for it in items]
            self.assertEqual(seqs, sorted(seqs))
            self.assertTrue(all(s >= 1 for s in seqs))

    def test_flat_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            relpath = f"sessions/2025/09/10/rollout-2025-09-10T00-00-00-{SID}.jsonl"
            path = pathlib.Path(tmp) / "codex" / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(path, codex_flat_records())
            state, items, offset, line_no = ab.extract_codex(path, relpath)
            self.assertEqual(state.session_id, SID)
            self.assertEqual(state.git_branch, "main")
            kinds = [it["kind"] for it in items]
            self.assertIn("reasoning_summary", kinds)
            self.assertIn("function_call", kinds)
            self.assertIn("function_call_output", kinds)
            # record_type: state separators must be skipped
            self.assertNotIn("state", kinds)
            rsum = next(it for it in items if it["kind"] == "reasoning_summary")
            self.assertEqual(rsum["content"], "flat reasoning")


class WriteAndSearchTests(unittest.TestCase):
    def _db(self, tmp):
        db = pathlib.Path(tmp) / "test.db"
        conn = ab.connect(db, readonly=False)
        return conn, db

    def test_idempotent_write_and_fts(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, db = self._db(tmp)
            path, relpath = make_claude_file(pathlib.Path(tmp))
            session, items = ab.extract_claude(path, relpath)
            n1 = ab.write_file_batch(conn, "claude", session, items, relpath)
            conn.commit()
            n2 = ab.write_file_batch(conn, "claude", session, items, relpath)
            conn.commit()
            self.assertEqual(n2, 0)  # second write inserts nothing new
            total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            self.assertEqual(total, n1)

            row = conn.execute(
                "SELECT item_id FROM items_fts WHERE items_fts MATCH ? LIMIT 1",
                ('"Fernet"',),
            ).fetchone()
            self.assertIsNotNone(row)
            conn.close()

    def test_session_upsert_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, db = self._db(tmp)
            path, relpath = make_claude_file(pathlib.Path(tmp))
            session, items = ab.extract_claude(path, relpath)
            ab.write_file_batch(conn, "claude", session, items, relpath)
            conn.commit()
            srow = conn.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(srow["session_id"], SID)
            self.assertGreater(srow["n_items"], 0)
            conn.close()

    def test_cmd_search_finds_indexed_reasoning(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as tmp:
            conn, db = self._db(tmp)
            path, relpath = make_claude_file(pathlib.Path(tmp))
            session, items = ab.extract_claude(path, relpath)
            ab.write_file_batch(conn, "claude", session, items, relpath)
            conn.commit()
            conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ab.main(["--db", str(db), "search", "Fernet", "--limit", "3"])
            self.assertIn("i:claude:", buf.getvalue())


class ResumeTests(unittest.TestCase):
    def test_line_number_continues_from_start_line_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            relpath = f"sessions/2025/11/01/rollout-x-{SID}.jsonl"
            path = pathlib.Path(tmp) / "codex" / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            write_jsonl(path, codex_envelope_records())
            # Full parse: session_meta (line 1) and turn_context (line 2) produce
            # no items, so the first item is from line 3.
            _state, items_full, _off, ln_full = ab.extract_codex(path, relpath)
            self.assertEqual(min(it["seq"] for it in items_full), 3)
            self.assertEqual(ln_full, 10)
            # Resume seeded at line 100: the same first item now sits at line 103.
            _s2, items_resumed, _o2, ln2 = ab.extract_codex(path, relpath, start_offset=0, start_line_no=100)
            self.assertEqual(min(it["seq"] for it in items_resumed), 103)
            self.assertEqual(ln2, 100 + ln_full)


if __name__ == "__main__":
    unittest.main()
