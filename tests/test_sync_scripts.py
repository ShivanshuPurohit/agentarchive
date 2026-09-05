import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cloud = load("plain_cloud_backup", "scripts/plain_cloud_backup.py")
portable = load("export_portable_state", "scripts/export_portable_state.py")
ssd_seed = load("create_ssd_seed", "scripts/create_ssd_seed.py")
syncthing = load("configure_syncthing", "scripts/configure_syncthing.py")


class SyncScriptTests(unittest.TestCase):
    def test_seed_uses_configured_legacy_paths_cutoffs_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "older-archive"
            database = legacy / "analysis/cutoff/claude_index.sqlite"
            database.parent.mkdir(parents=True)
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE evidence(value TEXT)")
            conn.execute("INSERT INTO evidence VALUES('retained')")
            conn.commit()
            conn.close()
            handoff = legacy / "analysis/complete/START_HERE.md"
            handoff.parent.mkdir(parents=True)
            handoff.write_text("# Earlier history\n")
            raw_relative = "source_snapshot/claude_home_fixture/projects"
            raw = legacy / raw_relative / "session.jsonl"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps({"cwd": str(root / "public")}) + "\n")
            config = {
                "sources": {"claude": {"legacy_cutoff_utc": "2024-01-01T00:00:00Z"}},
                "excluded_workspaces": [str(pathlib.Path.home() / "private-work")],
                "legacy_indexes": [{
                    "harness": "claude", "name": "Earlier history", "workspace": str(legacy),
                    "database": str(database), "start_here": str(handoff), "seed_paths": [raw_relative],
                }],
            }
            snapshot = root / "snapshot"
            ssd_seed.legacy_snapshot(config, snapshot)
            self.assertEqual((snapshot / "legacy/claude" / raw_relative / "session.jsonl").read_bytes(), raw.read_bytes())
            portable_source = root / "portable-source"
            portable_source.mkdir()
            portable.backup_catalog(database, portable_source / "catalog.sqlite")
            ssd_seed.portable_snapshot(portable_source, snapshot, config)
            emitted = json.loads((snapshot / "portable/config.wsl-readonly.json").read_text())
            self.assertEqual(emitted["sources"]["claude"]["legacy_cutoff_utc"], "2024-01-01T00:00:00Z")
            self.assertIn("~/private-work", emitted["excluded_workspaces"])
            self.assertEqual(emitted["legacy_indexes"][0]["database"],
                             "~/.local/share/AgentArchiveImports/current/legacy/claude/analysis/cutoff/claude_index.sqlite")
            self.assertNotIn(str(legacy), json.dumps(emitted))

    def test_seed_without_historical_archives_still_has_a_valid_legacy_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot = pathlib.Path(temp)
            ssd_seed.legacy_snapshot({}, snapshot)
            self.assertTrue((snapshot / "legacy").is_dir())

    def test_ssd_seed_makes_wal_database_self_contained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.sqlite"
            destination = root / "portable.sqlite"
            connection = sqlite3.connect(source)
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            connection.execute("CREATE TABLE evidence(value TEXT)")
            connection.execute("INSERT INTO evidence VALUES('retained')")
            connection.commit()
            ssd_seed.portable_sqlite_copy(source, destination)
            connection.close()
            copied = sqlite3.connect(destination)
            try:
                self.assertEqual(copied.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                self.assertEqual(
                    copied.execute("SELECT value FROM evidence").fetchone()[0], "retained"
                )
            finally:
                copied.close()

    def test_plain_cloud_sources_count_links_and_join_device_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "raw.jsonl").write_bytes(b"evidence")
            (root / "raw-link").symlink_to("raw.jsonl")
            files, bytes_total = cloud.source_stats(root)
            self.assertEqual(files, 2)
            self.assertGreaterEqual(bytes_total, len(b"evidence"))
            filtered_files, _ = cloud.source_stats(root, ("raw.jsonl",))
            self.assertEqual(filtered_files, 1)
            self.assertEqual(
                cloud.remote_join("drive:AgentArchive/", "devices", "mac", "current"),
                "drive:AgentArchive/devices/mac/current",
            )

    def test_plain_cloud_apply_uses_drive_backend_and_writes_verified_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake = binary_dir / "rclone"
            fake.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = config ]; then printf '[drive]\\ntype = drive\\n'; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            claude = root / "claude"
            codex = root / "codex"
            claude.mkdir()
            codex.mkdir()
            (claude / "history.jsonl").write_text("claude\n", encoding="utf-8")
            (codex / "history.jsonl").write_text("codex\n", encoding="utf-8")
            receipt_dir = root / "receipts"
            environment = os.environ.copy()
            environment["PATH"] = str(binary_dir) + os.pathsep + environment.get("PATH", "")
            result = subprocess.run(
                [
                    str(ROOT / "scripts/plain_cloud_backup.py"),
                    "--remote", "drive:AgentArchive",
                    "--device-id", "wsl-test",
                    "--claude-home", str(claude),
                    "--codex-home", str(codex),
                    "--receipt-dir", str(receipt_dir),
                    "--acknowledge-plaintext-credentials",
                    "--acknowledge-excluded-workspace-history",
                    "--apply",
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "complete")
            self.assertEqual(len(output["operations"]), 2)
            self.assertTrue(pathlib.Path(output["receipt"]).is_file())

    def test_portable_catalog_export_is_a_consistent_independent_sqlite_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "live.sqlite"
            destination = root / "portable.sqlite"
            conn = sqlite3.connect(source)
            conn.execute("CREATE TABLE evidence(value TEXT)")
            conn.execute("INSERT INTO evidence VALUES('retained')")
            conn.commit()
            portable.backup_catalog(source, destination)
            conn.execute("INSERT INTO evidence VALUES('later')")
            conn.commit()
            conn.close()
            copied = sqlite3.connect(destination)
            try:
                values = [row[0] for row in copied.execute("SELECT value FROM evidence")]
            finally:
                copied.close()
            self.assertEqual(values, ["retained"])

    def test_syncthing_keeps_raw_bidirectional_but_catalog_directional(self):
        root = pathlib.Path("/tmp/example")
        source = syncthing.folder_definitions(
            root / ".claude",
            root / ".codex",
            root / "portable",
            "source",
        )
        receiver = syncthing.folder_definitions(
            root / ".claude",
            root / ".codex",
            root / "portable",
            "receiver",
        )
        self.assertTrue(all(row["type"] == "sendreceive" for row in source[:5]))
        self.assertEqual(source[-1]["type"], "sendonly")
        self.assertEqual(receiver[-1]["type"], "receiveonly")
        config = {"excluded_workspaces": []}
        patterns = syncthing.privacy_patterns(config, source)
        self.assertIn(
            "/catalog.sqlite-wal", patterns["agentarchive-portable-state"]
        )

    def test_syncthing_locally_ignores_excluded_workspace_rollouts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            claude = root / ".claude"
            codex = root / ".codex"
            folders = syncthing.folder_definitions(claude, codex)
            for folder in folders:
                pathlib.Path(folder["path"]).mkdir(parents=True)
            private = root / "private"
            claude_rollout = claude / "projects/private-project/session.jsonl"
            claude_rollout.parent.mkdir(parents=True)
            claude_rollout.write_text(
                json.dumps({"cwd": str(private), "message": "not exported"}) + "\n",
                encoding="utf-8",
            )
            session_id = "12345678-1234-1234-1234-123456789abc"
            codex_rollout = codex / f"sessions/2026/08/30/rollout-{session_id}.jsonl"
            codex_rollout.parent.mkdir(parents=True)
            codex_rollout.write_text(
                json.dumps({"payload": {"cwd": str(private)}}) + "\n",
                encoding="utf-8",
            )
            attachment = codex / "attachments" / session_id / "result.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("retained locally", encoding="utf-8")
            config = {"excluded_workspaces": [str(private)]}
            patterns = syncthing.privacy_patterns(config, folders)
            self.assertEqual(
                patterns["agentarchive-claude-projects"], ["/private-project"]
            )
            self.assertIn(
                f"/{session_id}/result.txt",
                patterns["agentarchive-codex-attachments"],
            )
            syncthing.write_privacy_ignores(folders, patterns, root / "backups")
            ignore = (claude / "projects/.stignore").read_text(encoding="utf-8")
            self.assertIn("agentarchive-private-workspaces:start", ignore)
            self.assertIn("/private-project", ignore)


if __name__ == "__main__":
    unittest.main()
