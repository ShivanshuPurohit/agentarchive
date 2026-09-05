"""Behavior tests for normalized reasoning extraction and AgentArchive consumption."""

import json
import pathlib
import sqlite3
import tempfile
import unittest

import agent_archive as aa
import reasoning_index as ri


CHILD = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"


def append_jsonl(path: pathlib.Path, records, trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for index, record in enumerate(records):
            raw = json.dumps(record).encode()
            handle.write(raw)
            if trailing_newline or index < len(records) - 1:
                handle.write(b"\n")


def fixture(stage: pathlib.Path) -> pathlib.Path:
    path = (
        stage / "data" / "codex" / "sessions" / "2026" / "08" / "12"
        / f"rollout-2026-08-12T00-00-00-{CHILD}.jsonl"
    )
    append_jsonl(
        path,
        [
            {
                "timestamp": "2026-08-12T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": CHILD, "parent_thread_id": PARENT},
            },
            # Fork files can embed copied parent headers. They must never
            # overwrite the file owner's provenance identity.
            {
                "timestamp": "2026-08-12T00:00:01Z",
                "type": "session_meta",
                "payload": {"id": PARENT},
            },
            {
                "timestamp": "2026-08-12T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Reject the resident kernel."},
            },
            {
                "timestamp": "2026-08-12T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Complete the rung card first."}
                    ],
                    "encrypted_content": "gAAAAAB-opaque",
                },
            },
            {
                "timestamp": "2026-08-12T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Reject the resident kernel."},
            },
        ],
    )
    return path


class ReasoningIndexTests(unittest.TestCase):
    def connect(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ri.ensure_schema(conn)
        return conn

    def test_normalizes_payloads_and_keeps_child_file_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = pathlib.Path(temp)
            fixture(stage)
            conn = self.connect()
            result = ri.index_stage(conn, stage, "capture-1")

            self.assertEqual(result["payloads"], 2)
            self.assertEqual(result["occurrences"], 3)
            self.assertEqual(result["opaque_items"], 1)
            owners = {
                row[0] for row in conn.execute("SELECT DISTINCT session_id FROM reasoning_occurrences")
            }
            self.assertEqual(owners, {CHILD})

            payload = conn.execute(
                "SELECT payload_id FROM reasoning_payloads WHERE text LIKE 'Reject%'"
            ).fetchone()[0]
            evidence = ri.context(conn, payload)
            self.assertEqual(evidence["occurrence_count"], 2)

            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("reasoning_fts_content", names)
            self.assertEqual(ri.search(conn, '"resident"', 5)[0]["occurrence_count"], 2)

            again = ri.index_stage(conn, stage, "capture-2")
            self.assertEqual(again["skipped"], 1)
            self.assertEqual(again["occurrences"], 0)
            conn.close()

    def test_incomplete_line_is_retried_after_append(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = pathlib.Path(temp)
            path = fixture(stage)
            partial = {
                "timestamp": "2026-08-12T00:00:05Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Wait for completion."},
            }
            append_jsonl(path, [partial], trailing_newline=False)
            conn = self.connect()
            first = ri.index_stage(conn, stage, "capture-1")
            self.assertEqual(first["payloads"], 2)

            with path.open("ab") as handle:
                handle.write(b"\n")
            second = ri.index_stage(conn, stage, "capture-2")
            self.assertEqual(second["payloads"], 1)
            self.assertEqual(second["occurrences"], 1)
            conn.close()

    def test_agent_archive_reasoning_search_consumes_the_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["legacy_indexes"] = []
            config["sources"]["claude"]["root"] = str(root / "claude")
            config["sources"]["codex"]["root"] = str(root / "codex")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            payload_id, _ = ri._payload_id(conn, "Complete the rung card before implementation.")
            conn.execute(
                """
                INSERT INTO reasoning_occurrences(
                    payload_id,harness,session_id,source_relpath,line_no,timestamp,kind,capture_id
                ) VALUES(?,'codex',?,?,3,?,'reasoning_summary','capture-1')
                """,
                (
                    payload_id, CHILD,
                    f"sessions/2026/08/12/rollout-{CHILD}.jsonl",
                    "2026-08-12T00:00:03Z",
                ),
            )
            conn.commit()
            conn.close()

            hits = aa.search_archive(config, "rung card", reasoning_only=True)
            self.assertEqual(hits[0]["hit_id"], f"r:codex:{payload_id}")
            expanded = aa.indexed_context(config, hits[0]["hit_id"], max_chars=2000)
            self.assertIn("Complete the rung card", expanded)
            self.assertIn(f"session={CHILD}", expanded)


if __name__ == "__main__":
    unittest.main()
