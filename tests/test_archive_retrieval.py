"""Retrieval behavior preserved from the original archive test suite."""

import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock
import agent_archive as aa
from agentarchive import catalog


class ArchiveRetrievalTests(unittest.TestCase):
    def test_legacy_codex_raw_path_relocates_a_moved_archive_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = pathlib.Path(temp) / "legacy" / "codex"
            database = legacy / "analysis" / "complete" / "codex_evidence.sqlite"
            database.parent.mkdir(parents=True)
            database.touch()
            raw = (
                legacy
                / "source_snapshot"
                / "codex_home_fixture"
                / "sessions"
                / "2026"
                / "07"
                / "03"
                / "rollout.jsonl"
            )
            raw.parent.mkdir(parents=True)
            raw.write_text("{}\n", encoding="utf-8")
            original = (
                "/Users/example/Archives/codex/source_snapshot/"
                "codex_home_fixture/sessions/2026/07/03/rollout.jsonl"
            )
            self.assertEqual(aa.legacy_codex_raw_path(database, original), raw.resolve())

    def test_search_is_read_only_and_punctuation_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["legacy_indexes"] = []
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            conn.execute(
                "INSERT INTO blocks_fts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "The recorded fix uses NCU 2026.2.1, not stale 2025.3.1.",
                    "codex",
                    "session-1",
                    "raw.jsonl",
                    42,
                    1,
                    "assistant",
                    "text",
                    "",
                ),
            )
            conn.commit()
            conn.close()
            catalog = aa.catalog_path(config)
            before = catalog.stat().st_mtime_ns
            with mock.patch.object(aa.os, "chmod") as chmod:
                results = aa.search_archive(config, "NCU 2025.3.1 versus 2026.2", 10)
            self.assertTrue(results)
            self.assertEqual(results[0]["line_no"], 42)
            self.assertEqual(
                results[0]["raw_path"],
                str(pathlib.Path(config["sources"]["codex"]["root"]) / "raw.jsonl"),
            )
            self.assertEqual(catalog.stat().st_mtime_ns, before)
            chmod.assert_not_called()

    def test_explicit_boolean_search_syntax_is_preserved(self):
        self.assertEqual(
            aa.normalized_fts_query('"2025.3.1" AND "2026.2"'),
            '"2025.3.1" AND "2026.2"',
        )
        self.assertEqual(
            aa.normalized_fts_query("NCU AND 2025.3.1 AND stale"),
            '"NCU" AND "2025.3.1" AND "stale"',
        )

    def test_natural_search_prefers_multi_term_evidence_over_or_distractors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["legacy_indexes"] = []
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            rows = (
                ("This only says versus.", "claude", "noise", "noise.jsonl", 1, 1, "assistant", "text", ""),
                ("NCU 2025.3.1 was stale; 2026.2 fixed TP4.", "codex", "answer", "answer.jsonl", 53, 1, "assistant", "text", ""),
            )
            conn.executemany("INSERT INTO blocks_fts VALUES(?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            conn.close()
            results = aa.search_archive(
                config, "NCU 2025.3.1 versus latest stable 2026.2", 10
            )
            answer = next(item for item in results if item.get("session_id") == "answer")
            self.assertFalse(any(item.get("session_id") == "noise" for item in results))
            self.assertEqual(answer["match_mode"], "all-signal")

    def test_indexed_context_expands_only_selected_coordinate_under_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            codex_root = root / "codex"
            config["sources"]["codex"]["root"] = str(codex_root)
            raw = codex_root / "sessions/raw.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_text("fixture\n", encoding="utf-8")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            conn.execute(
                """
                INSERT INTO blocks(
                    harness,session_id,source_relpath,line_no,block_index,timestamp,
                    role,kind,tool_name,text,text_sha256,capture_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "codex", "session-1", "sessions/raw.jsonl", 42, 1,
                    "2026-08-01T00:00:00Z", "assistant", "text", "",
                    "decisive evidence " * 100, "abc", "capture-1",
                ),
            )
            conn.commit()
            conn.close()
            result = aa.indexed_context(config, f"{raw}:42", max_chars=300)
            self.assertIn(f"{raw.resolve()}:42#1", result)
            self.assertIn("decisive evidence", result)
            self.assertIn("TRUNCATED", result)
            self.assertNotIn("fixture", result)
            self.assertLessEqual(len(result), 300)

    def test_indexed_context_accepts_incremental_hit_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["sources"]["codex"]["root"] = str(root / "codex")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            cursor = conn.execute(
                """
                INSERT INTO blocks(
                    harness,session_id,source_relpath,line_no,block_index,role,kind,
                    text,text_sha256,capture_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "codex", "session-1", "sessions/raw.jsonl", 7, 0,
                    "assistant", "text", "selected exact evidence", "abc", "capture",
                ),
            )
            block_id = cursor.lastrowid
            conn.commit()
            conn.close()
            result = aa.indexed_context(config, f"i:codex:{block_id}", max_chars=512)
            self.assertIn(f"target=i:codex:{block_id}", result)
            self.assertIn("selected exact evidence", result)
            self.assertLessEqual(len(result), 512)

    def test_exact_session_id_uses_metadata_instead_of_fifty_content_mentions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["legacy_indexes"] = []
            codex_root = root / "codex"
            config["sources"]["codex"]["root"] = str(codex_root)
            aa.ensure_private_tree(root / "state")
            session_id = "11111111-1111-4111-8111-111111111111"
            raw = codex_root / f"sessions/2026/08/01/rollout-{session_id}.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_text("fixture\n", encoding="utf-8")
            conn = aa.connect_catalog(config)
            conn.execute(
                """
                INSERT INTO blocks(
                    harness,session_id,source_relpath,line_no,block_index,role,kind,
                    text,text_sha256,capture_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "codex", session_id, str(raw.relative_to(codex_root)), 7, 0,
                    "user", "text", "Investigate the profiler failure", "abc", "capture",
                ),
            )
            conn.commit()
            conn.close()
            results = aa.search_archive(config, session_id, 8)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["match_mode"], "session-id")
            self.assertEqual(results[0]["session_id"], session_id)
            self.assertEqual(results[0]["raw_path"], str(raw.resolve()))
            self.assertIsNone(results[0]["line_no"])
            self.assertEqual(results[0]["excerpt"], "exact raw transcript filename match")

    def test_terse_coordinate_omits_repeated_absolute_path(self):
        session_id = "11111111-1111-4111-8111-111111111111"
        item = {
            "raw_path": f"/Users/example/.codex/sessions/rollout-{session_id}.jsonl",
            "line_no": 99,
            "match_mode": "all-signal",
        }
        self.assertEqual(aa.terse_search_coordinate(item), f"{session_id}:99")
        item["match_mode"] = "session-id"
        self.assertIn("/Users/example/", aa.terse_search_coordinate(item))

    def test_search_results_are_balanced_across_harnesses_and_indexes(self):
        rows = [
            *({"harness": "claude", "source": "incremental", "n": n} for n in range(10)),
            {"harness": "codex", "source": "incremental", "n": 10},
            {"harness": "claude", "source": "historical", "n": 11},
            {"harness": "codex", "source": "historical", "n": 12},
        ]
        balanced = aa.balanced_search_results(rows, 4)
        self.assertEqual(
            {(row["harness"], row["source"]) for row in balanced},
            {
                ("claude", "incremental"),
                ("codex", "incremental"),
                ("claude", "historical"),
                ("codex", "historical"),
            },
        )

    def test_summary_results_fill_only_after_raw_evidence(self):
        rows = [
            {"harness": "claude", "source": "incremental", "session_id": "raw-c"},
            {"harness": "codex", "source": "incremental", "session_id": "raw-x"},
            {"harness": "claude", "source": "historical", "session_id": "old-c"},
            {"harness": "claude", "source": "summary", "path": "summary.md"},
        ]
        first = aa.balanced_search_results(rows, 3)
        self.assertTrue(all(item["source"] != "summary" for item in first))
        filled = aa.balanced_search_results(rows, 4)
        self.assertEqual(filled[-1]["source"], "summary")

    def test_indexed_context_accepts_historical_codex_hit_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            aa.connect_catalog(config).close()
            legacy = root / "legacy.sqlite"
            raw = root / "archived.jsonl"
            raw.write_text("fixture\n", encoding="utf-8")
            conn = sqlite3.connect(legacy)
            conn.executescript(
                """
                CREATE TABLE files(file_id INTEGER PRIMARY KEY, source_path TEXT);
                CREATE TABLE events(
                    event_id INTEGER PRIMARY KEY,file_id INTEGER,line_no INTEGER,raw_sha256 TEXT
                );
                CREATE TABLE blocks(
                    block_id INTEGER PRIMARY KEY,event_id INTEGER,file_id INTEGER,role TEXT,
                    kind TEXT,name TEXT,text TEXT,text_sha256 TEXT,timestamp TEXT
                );
                """
            )
            conn.execute("INSERT INTO files VALUES(1,?)", (str(raw),))
            conn.execute("INSERT INTO events VALUES(1,1,19,'raw-sha')")
            conn.execute(
                "INSERT INTO blocks VALUES(7,1,1,'assistant','text','',?,?,?)",
                ("historical exact evidence", "text-sha", "2026-01-01T00:00:00Z"),
            )
            conn.commit()
            conn.close()
            config["legacy_indexes"] = [
                {"harness": "codex", "database": str(legacy)}
            ]
            result = aa.indexed_context(config, "l:codex:7", max_chars=512)
            self.assertIn("target=l:codex:7", result)
            self.assertIn(f"{raw}:19#0", result)
            self.assertIn("historical exact evidence", result)
            self.assertLessEqual(len(result), 512)
