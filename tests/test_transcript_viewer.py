import json
import pathlib
import tempfile
import unittest

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from transcript_model import SourceIndex, transcript_page
from transcript_viewer import Registry


def write_jsonl(path, rows, final_newline=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row) for row in rows)
    path.write_text(text + ("\n" if final_newline else ""), encoding="utf-8")


class TranscriptModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_claude_title_reasoning_tool_join_and_safe_tail(self):
        path = self.root / "c.jsonl"
        rows = [
            {"type": "ai-title", "aiTitle": "Wrong later title", "sessionId": "c"},
            {"type": "custom-title", "customTitle": "Chosen title", "sessionId": "c"},
            {"type": "assistant", "sessionId": "c", "message": {"model": "claude-test", "content": [
                {"type": "thinking", "thinking": "private hypothesis", "signature": "DO_NOT_SHOW"},
                {"type": "tool_use", "id": "call-a", "name": "Bash", "input": {"command": "false"}},
            ]}},
            {"type": "user", "sessionId": "c", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "call-a", "is_error": True, "content": "failed"}
            ]}, "toolUseResult": {"stderr": "bad"}},
        ]
        write_jsonl(path, rows)
        index = SourceIndex("claude", path)
        page = transcript_page(index, 1, 20)
        self.assertEqual(index.title(), "Chosen title")
        self.assertEqual(index.owner, "c")
        self.assertEqual(len(index.calls["call-a"]), 3)
        tool = next(item for item in page["items"] if item["kind"] == "tool")
        self.assertEqual(tool["status"], "error")
        self.assertIn("private hypothesis", json.dumps(page))
        self.assertNotIn("DO_NOT_SHOW", json.dumps(page))

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "sessionId": "c", "message": {"content": "later"}}))
        index.refresh()
        self.assertEqual(index.line_count, 4)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        index.refresh()
        self.assertEqual(index.line_count, 5)

    def test_codex_first_header_ownership_interleaving_and_opaque_reasoning(self):
        path = self.root / "rollout-child.jsonl"
        header = {"type": "session_meta", "payload": {"id": "child", "session_id": "root", "parent_thread_id": "parent"}}
        copied = {"type": "session_meta", "payload": {"id": "parent", "session_id": "parent"}}
        rows = [header, copied]
        for call in ("a", "b", "c"):
            rows.append({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                         "arguments": json.dumps({"cmd": call}), "call_id": call}})
        rows.extend([
            {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "b", "exit_code": 1}},
            {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "a", "exit_code": 0}},
            {"type": "event_msg", "payload": {"type": "exec_command_end", "call_id": "c", "exit_code": 0}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "a", "output": "A"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "b", "output": "B"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": "C"}},
            {"type": "response_item", "payload": {"type": "reasoning", "id": "r1",
                         "summary": [{"type": "summary_text", "text": "readable"}],
                         "encrypted_content": "SECRET_CIPHERTEXT"}},
        ])
        write_jsonl(path, rows)
        index = SourceIndex("codex", path)
        page = transcript_page(index, 1, 30)
        self.assertEqual((index.owner, index.root, index.parent), ("child", "root", "parent"))
        tools = {item["call_id"]: item for item in page["items"] if item["kind"] == "tool"}
        self.assertEqual(set(tools), {"a", "b", "c"})
        self.assertEqual(tools["b"]["status"], "error")
        self.assertTrue(all(len(tool["components"]) == 3 for tool in tools.values()))
        focused = transcript_page(index, 1, 30, focus=10)
        self.assertEqual(focused["focus_line"], 4)
        encoded = json.dumps(page)
        self.assertIn("readable", encoded)
        self.assertIn("encrypted reasoning", encoded)
        self.assertNotIn("SECRET_CIPHERTEXT", encoded)

    def test_claude_meta_user_messages_are_verbose(self):
        path = self.root / "meta.jsonl"
        write_jsonl(path, [
            {"type": "user", "sessionId": "meta", "message": {"content": "visible prompt"}},
            {"type": "user", "sessionId": "meta", "isMeta": True,
             "message": {"content": "# Injected skill reference"}},
        ])
        items = transcript_page(SourceIndex("claude", path), 1, 20)["items"]
        self.assertEqual([item["visibility"] for item in items], ["summary", "verbose"])

    def test_codex_task_lifecycle_metadata_is_verbose(self):
        path = self.root / "lifecycle.jsonl"
        write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": "lifecycle"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
        ])
        items = transcript_page(SourceIndex("codex", path), 1, 20)["items"]
        lifecycle = [item for item in items if item.get("title", "").startswith("task_")]
        self.assertEqual([item["visibility"] for item in lifecycle], ["verbose", "verbose"])

    def test_registry_uses_custom_claude_and_codex_index_titles(self):
        claude, codex = self.root / ".claude", self.root / ".codex"
        cpath = claude / "projects" / "-tmp" / "11111111-1111-1111-1111-111111111111.jsonl"
        write_jsonl(cpath, [
            {"type": "user", "sessionId": cpath.stem, "message": {"content": "hello"}},
            {"type": "assistant", "sessionId": cpath.stem, "cwd": "/tmp/project",
             "message": {"model": "claude-test", "content": "working"}},
            {"type": "ai-title", "aiTitle": "AI title", "sessionId": cpath.stem},
            {"type": "custom-title", "customTitle": "Custom title", "sessionId": cpath.stem},
        ])
        child = cpath.parent / cpath.stem / "subagents" / "workflows" / "wf-1" / "agent-a.jsonl"
        write_jsonl(child, [{"type": "user", "agentId": "agent-a", "sessionId": cpath.stem,
                            "message": {"content": "Review the launch code for real defects"}}])
        kpath = codex / "sessions" / "2026" / "01" / "01" / "rollout-22222222-2222-2222-2222-222222222222.jsonl"
        write_jsonl(kpath, [{"type": "session_meta", "payload": {"id": "22222222-2222-2222-2222-222222222222"}}])
        (codex / "session_index.jsonl").parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(codex / "session_index.jsonl", [{"id": "22222222-2222-2222-2222-222222222222", "thread_name": "Codex title"}])
        config = {"state_root": str(self.root / "state"), "legacy_indexes": [], "sources": {
            "claude": {"root": str(claude)}, "codex": {"root": str(codex)}}}
        registry = Registry(config)
        claude_rows = registry.sessions("Custom title", "claude", False, "", 10)["sessions"]
        codex_rows = registry.sessions("Codex title", "codex", False, "", 10)["sessions"]
        self.assertEqual(claude_rows[0]["title"], "Custom title")
        self.assertEqual(claude_rows[0]["project"], "/tmp/project")
        self.assertEqual(claude_rows[0]["model"], "claude-test")
        self.assertEqual(codex_rows[0]["title"], "Codex title")
        children = registry.sessions("Review the launch code", "claude", True, "", 10)["sessions"]
        self.assertEqual(children[0]["title"], "Review the launch code for real defects")


if __name__ == "__main__":
    unittest.main()
