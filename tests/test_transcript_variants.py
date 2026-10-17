"""A transcript and the `.pre-import` original beside it are two sources of one session."""

import json
import pathlib
import tempfile
import unittest

import agent_archive as aa
from agentarchive.context import indexed_context
from agentarchive.search import terse_search_coordinate
from transcript_viewer import Registry

SESSION = "55555555-5555-4555-8555-555555555555"
PRIVATE = "66666666-6666-4666-8666-666666666666"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def transcript(session, cwd, original):
    """A prompt and its reply; the original keeps the thought the desktop's rewrite drops."""
    reply = [{"type": "text", "text": "Rename the retry helper"}]
    if original:
        reply.insert(0, {"type": "thinking", "thinking": "Zebra quokka plan: the helper hides a bug"})
    return [
        {"type": "user", "sessionId": session, "cwd": cwd, "timestamp": "2026-10-01T10:00:00Z",
         "message": {"role": "user", "content": "Please tidy the retry helper"}},
        {"type": "assistant", "sessionId": session, "cwd": cwd, "timestamp": "2026-10-01T10:00:05Z",
         "message": {"role": "assistant", "model": "claude-test", "content": reply}},
    ]


class TranscriptVariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.config = aa.default_config(self.root / "state")
        self.config["legacy_indexes"] = []
        self.config["excluded_workspaces"] = [str(self.root / "private")]
        for harness, definition in self.config["sources"].items():
            definition.update(root=str(self.root / harness), paths=[definition["paths"][0]], sqlite=[])
        aa.ensure_private_tree(self.root / "state")
        aa.connect_catalog(self.config).close()
        self.canonical = self.root / "claude" / "projects" / "-work" / f"{SESSION}.jsonl"
        self.original = self.canonical.with_name(self.canonical.name + ".pre-import")
        write_jsonl(self.canonical, transcript(SESSION, str(self.root / "work"), original=False))
        write_jsonl(self.original, transcript(SESSION, str(self.root / "work"), original=True))

    def tearDown(self):
        self.temp.cleanup()

    def index(self):
        stage = self.root / "stage"
        warnings = aa.stage_sources(self.config, stage)
        conn = aa.connect_catalog(self.config)
        aa.index_capture(self.config, conn, stage, "capture-1", [])
        conn.commit()
        return conn, stage, warnings

    def test_original_indexes_at_its_own_coordinate_while_the_session_follows_the_canonical_file(self):
        conn, _, _ = self.index()
        canonical_rel = self.canonical.relative_to(self.root / "claude").as_posix()
        original_rel = canonical_rel + ".pre-import"
        try:
            relpaths = sorted(row[0] for row in conn.execute("SELECT relpath FROM indexed_files"))
            self.assertEqual(relpaths, [canonical_rel, original_rel])
            session = conn.execute("SELECT source_relpath FROM sessions WHERE session_id=?", (SESSION,)).fetchall()
            self.assertEqual([row[0] for row in session], [canonical_rel])
            per_file = dict(conn.execute("SELECT source_relpath, COUNT(*) FROM blocks GROUP BY 1"))
            self.assertEqual(per_file, {canonical_rel: 2, original_rel: 3})
        finally:
            conn.close()

        # The thought survives only in the original, at a coordinate that names it.
        thought = aa.search_archive(self.config, "zebra quokka")
        self.assertEqual([hit["raw_path"] for hit in thought], [str(self.original)])
        self.assertEqual(terse_search_coordinate(thought[0]), f"{SESSION}.pre-import:2")
        expanded = indexed_context(self.config, thought[0]["hit_id"])
        self.assertIn(f"{self.original.resolve()}:2#0", expanded)
        self.assertIn("Zebra quokka plan", expanded)

        # An event both files hold is one hit, from the canonical file.
        shared = aa.search_archive(self.config, "tidy retry helper")
        self.assertEqual({hit["raw_path"] for hit in shared}, {str(self.canonical)})
        hashes = [hit["text_sha256"] for hit in shared]
        self.assertEqual(len(hashes), len(set(hashes)))
        exact = aa.search_archive(self.config, SESSION)
        self.assertEqual([hit["raw_path"] for hit in exact], [str(self.canonical.resolve())])

        # The sidebar lists the canonical file; the original is routable from a hit.
        registry = Registry(self.config)
        rows = registry.sessions("", "claude", True, "", 10)["sessions"]
        self.assertEqual([row["session_id"] for row in rows], [SESSION])
        self.assertEqual(registry.route_path(str(self.original)).kind, "pre-import")
        self.assertEqual(registry.route_path(str(self.canonical)).kind, "main")

    def test_private_original_is_scrubbed_from_the_stage_and_purged_with_its_session(self):
        private = self.root / "claude" / "projects" / "-private" / f"{PRIVATE}.jsonl"
        cwd = str(self.root / "private" / "repo")
        write_jsonl(private, transcript(PRIVATE, cwd, original=False))
        write_jsonl(private.with_name(private.name + ".pre-import"), transcript(PRIVATE, cwd, original=True))
        conn, stage, warnings = self.index()
        try:
            staged = {path.name for path in (stage / "data").rglob("*")}
            self.assertNotIn(private.name, staged)
            self.assertNotIn(private.name + ".pre-import", staged)
            self.assertIn(self.original.name, staged)
            self.assertTrue(any("claude=2" in warning for warning in warnings), warnings)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0], 2)

            # A workspace excluded after indexing takes every file that fed its session with it.
            self.config["excluded_workspaces"] = [str(self.root / "work")]
            self.assertEqual(aa.purge_excluded_sessions(self.config, conn), 1)
            for table in ("sessions", "blocks", "blocks_fts", "indexed_files"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
