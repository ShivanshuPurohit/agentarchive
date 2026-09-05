"""Preservation behavior preserved from the original archive test suite."""

import json
import inspect
import pathlib
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
import agent_archive as aa
from agentarchive import capture, metadata, recovery, storage


class ArchivePreservationTests(unittest.TestCase):
    def test_capture_never_scans_prior_staging_trees_implicitly(self):
        source = inspect.getsource(aa.capture)
        self.assertNotIn("recover_incomplete_stages", source)
        self.assertNotIn("audit_staging", source)
        self.assertIn("prior staging trees are not scanned", source)

    def test_raw_replica_manifest_detects_byte_changes_and_conflicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["excluded_workspaces"] = []
            for harness in ("claude", "codex"):
                source = root / harness
                source.mkdir()
                config["sources"][harness]["root"] = str(source)
            for definition in aa.raw_replica_definitions(config):
                folder = pathlib.Path(definition["path"])
                folder.mkdir(parents=True)
                (folder / "evidence.jsonl").write_text(
                    '{"tool_result":"complete"}\n', encoding="utf-8"
                )
            created = aa.write_raw_replica_manifest(config)
            manifest = pathlib.Path(created["manifest"])
            identical = aa.verify_raw_replica_manifest(config, manifest)
            self.assertEqual(identical["status"], "identical")
            changed_path = pathlib.Path(
                next(
                    definition["path"]
                    for definition in aa.raw_replica_definitions(config)
                    if definition["id"] == "agentarchive-codex-sessions"
                )
            ) / "evidence.jsonl"
            changed_path.write_text('{"tool_result":"changed!"}\n', encoding="utf-8")
            different = aa.verify_raw_replica_manifest(config, manifest)
            self.assertEqual(different["status"], "different")
            self.assertEqual(different["changed_count"], 1)
            conflict = changed_path.with_name("evidence.sync-conflict-20260830.jsonl")
            conflict.write_text("conflict\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "conflict files"):
                aa.write_raw_replica_manifest(config)

    def test_offsite_setup_refuses_excluded_history_without_explicit_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config = aa.default_config(pathlib.Path(temp) / "state")
            config["excluded_workspaces"] = ["/private/incognito"]
            aa.write_json(config_path, config)
            with mock.patch.object(storage, "restic") as restic_mock:
                with self.assertRaisesRegex(RuntimeError, "excluded workspaces"):
                    aa.configure_offsite(config_path, "rclone:drive:archive", None)
            restic_mock.assert_not_called()
            self.assertFalse(aa.read_json(config_path)["offsite"]["enabled"])

    def test_offsite_setup_migrates_and_verifies_before_committing_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config = aa.default_config(pathlib.Path(temp) / "state")
            config["excluded_workspaces"] = []
            aa.write_json(config_path, config)
            repository_config = json.dumps(
                {"id": "repo", "chunker_polynomial": "same-polynomial"}
            )
            source_rows = [{"id": "source-one"}, {"id": "source-two"}]
            destination_rows = [
                {"id": "dest-one", "original": "source-one"},
                {"id": "dest-two", "original": "source-two"},
            ]
            responses = [
                subprocess.CompletedProcess([], 0, "[]", ""),
                subprocess.CompletedProcess([], 0, repository_config, ""),
                subprocess.CompletedProcess([], 0, repository_config, ""),
                subprocess.CompletedProcess([], 0, json.dumps(source_rows), ""),
                subprocess.CompletedProcess([], 0, "copied", ""),
                subprocess.CompletedProcess([], 0, json.dumps(destination_rows), ""),
                subprocess.CompletedProcess([], 0, "no errors", ""),
            ]
            with mock.patch.object(storage, "restic", side_effect=responses) as restic_mock:
                result = aa.configure_offsite(
                    config_path,
                    "rclone:drive:AgentArchive/restic",
                    None,
                )
            committed = aa.read_json(config_path)
            self.assertTrue(committed["offsite"]["enabled"])
            self.assertEqual(result["local_snapshot_count"], 2)
            self.assertEqual(result["offsite_snapshot_count"], 2)
            self.assertTrue(result["migrated_existing"])
            copy_args = restic_mock.call_args_list[4].args[1]
            self.assertEqual(copy_args[0], "copy")
            self.assertIn("--from-repo", copy_args)
            self.assertIn("--host", copy_args)
            self.assertEqual(restic_mock.call_args_list[-1].kwargs["offsite"], True)

    def test_offsite_setup_failure_never_enables_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = pathlib.Path(temp) / "config.json"
            config = aa.default_config(pathlib.Path(temp) / "state")
            config["excluded_workspaces"] = []
            aa.write_json(config_path, config)
            responses = [
                subprocess.CompletedProcess([], 0, "[]", ""),
                subprocess.CompletedProcess(
                    [], 0, json.dumps({"id": "local", "chunker_polynomial": "a"}), ""
                ),
                subprocess.CompletedProcess(
                    [], 0, json.dumps({"id": "remote", "chunker_polynomial": "b"}), ""
                ),
            ]
            with mock.patch.object(storage, "restic", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "incompatible chunker"):
                    aa.configure_offsite(config_path, "remote:path", None)
            self.assertFalse(aa.read_json(config_path)["offsite"]["enabled"])

    def test_restic_check_removes_only_stale_locks_and_retries_once(self):
        locked = subprocess.CompletedProcess([], 11, "", "repository locked")
        unlocked = subprocess.CompletedProcess([], 0, "successfully removed 3 locks", "")
        checked = subprocess.CompletedProcess([], 0, "no errors", "")
        with mock.patch.object(
            storage,
            "restic",
            side_effect=[locked, unlocked, checked],
        ) as mocked:
            result, recovery = aa.restic_repository_check({}, ["check"])
        self.assertIs(result, checked)
        self.assertIn("removed 3 locks", recovery)
        self.assertEqual(mocked.call_args_list[1].args[1], ["unlock"])
        self.assertNotIn("--remove-all", mocked.call_args_list[1].args[1])
        self.assertEqual(mocked.call_args_list[2].args[1], ["check"])

    def test_restic_check_leaves_active_lock_as_failure(self):
        locked = subprocess.CompletedProcess([], 11, "", "repository locked")
        unlocked = subprocess.CompletedProcess([], 0, "successfully removed 0 locks", "")
        still_locked = subprocess.CompletedProcess([], 11, "", "repository locked")
        with mock.patch.object(
            storage,
            "restic",
            side_effect=[locked, unlocked, still_locked],
        ):
            result, recovery = aa.restic_repository_check({}, ["check"])
        self.assertEqual(result.returncode, 11)
        self.assertIn("removed 0 locks", recovery)

    def test_restic_check_does_not_unlock_other_failures(self):
        failed = subprocess.CompletedProcess([], 1, "", "repository damaged")
        with mock.patch.object(storage, "restic", return_value=failed) as mocked:
            result, recovery = aa.restic_repository_check({}, ["check"])
        self.assertIs(result, failed)
        self.assertIsNone(recovery)
        mocked.assert_called_once_with({}, ["check"], check=False)

    def test_backup_archive_runs_capture_then_verification(self):
        captured = {"status": "complete", "capture_id": "capture-1"}
        verified = {"status": "ok", "checks": {"restic": "ok"}}
        with mock.patch.object(capture, "capture", return_value=captured) as capture_mock:
            with mock.patch.object(capture, "verify_archive", return_value=verified) as verify_mock:
                result = aa.backup_archive({}, "manual-user-request")
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["capture"], captured)
        self.assertIs(result["verify"], verified)
        capture_mock.assert_called_once_with({}, "manual-user-request")
        verify_mock.assert_called_once_with({})

    def test_backup_uses_explicit_parent_snapshot(self):
        previous = "a" * 64
        created = "b" * 64
        listed = subprocess.CompletedProcess(
            [],
            0,
            json.dumps([
                {
                    "id": previous,
                    "time": "2026-07-25T00:00:00Z",
                    "tags": [
                        "agentarchive",
                        "local",
                        "capture-20260725T000000.000000Z",
                    ],
                },
                {
                    "id": "d" * 64,
                    "time": "2026-07-27T00:00:00Z",
                    "tags": [
                        "agentarchive",
                        "local",
                        "capture-20260724T000000.000000Z",
                    ],
                },
                {
                    "id": "c" * 64,
                    "time": "2026-07-21T00:00:00Z",
                    "tags": ["agentarchive", "local", "metadata-supplement"],
                },
            ]),
            "",
        )
        backed_up = subprocess.CompletedProcess(
            [], 0, json.dumps({"snapshot_id": created}) + "\n", ""
        )
        with tempfile.TemporaryDirectory() as temp:
            stage = pathlib.Path(temp)
            (stage / "data").mkdir()
            (stage / "meta").mkdir()
            with mock.patch.object(storage, "restic", side_effect=[listed, backed_up]) as mocked:
                snapshot = aa.backup_stage({}, stage, "capture")
        self.assertEqual(snapshot, created)
        backup_args = mocked.call_args_list[1].args[1]
        self.assertIn("--parent", backup_args)
        self.assertIn(previous, backup_args)
        self.assertNotIn("latest", backup_args)

    def test_sqlite_online_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.sqlite"
            destination = root / "dest.sqlite"
            conn = sqlite3.connect(source)
            conn.execute("CREATE TABLE values_table(value TEXT)")
            conn.execute("INSERT INTO values_table VALUES('treasure')")
            conn.commit()
            ok, detail = aa.sqlite_backup(source, destination)
            conn.close()
            self.assertTrue(ok, detail)
            self.assertEqual(list(root.glob(".dest.sqlite.tmp-*-wal")), [])
            self.assertEqual(list(root.glob(".dest.sqlite.tmp-*-shm")), [])
            copied = sqlite3.connect(destination)
            self.assertEqual(copied.execute("SELECT value FROM values_table").fetchone()[0], "treasure")
            copied.close()

    def test_platform_metadata_sidecar_serializes_raw_xattr_values(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = pathlib.Path(temp) / "stage"
            target = stage / "data" / "codex" / "session.jsonl"
            target.parent.mkdir(parents=True)
            target.write_text("{}\n", encoding="utf-8")
            xattrs = [
                {
                    "name": "com.example.provenance",
                    "size_bytes": 3,
                    "sha256": aa.hashlib.sha256(b"\x01\x02\x03").hexdigest(),
                    "value_base64": "AQID",
                }
            ]
            with mock.patch.object(
                metadata,
                "path_xattrs",
                return_value=(xattrs, None),
            ):
                path, digest, entries, warnings = aa.write_platform_metadata(stage)
            self.assertEqual(entries, 2)
            self.assertEqual(warnings, [])
            self.assertEqual(aa.sha256_file(path), digest)
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            file_row = next(row for row in rows if row["file_type"] == "file")
            self.assertEqual(
                file_row["xattrs"][0]["value_base64"],
                "AQID",
            )

    def test_snapshot_sentinel_verifies_platform_metadata_sidecar(self):
        metadata = '{"xattrs":[{"value_base64":"AQID"}]}\n'
        digest = aa.hashlib.sha256(metadata.encode("utf-8")).hexdigest()
        sentinel = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "capture_id": "capture",
                    "manifest_sha256": "manifest",
                    "platform_metadata_path": "platform-metadata.jsonl",
                    "platform_metadata_sha256": digest,
                }
            ),
            "",
        )
        sidecar = subprocess.CompletedProcess([], 0, metadata, "")
        with mock.patch.object(
            storage,
            "restic",
            side_effect=[sentinel, sidecar],
        ) as mocked:
            ok, detail = aa.verify_snapshot_sentinel(
                {},
                "snapshot",
                "capture",
                "manifest",
            )
        self.assertTrue(ok, detail)
        self.assertEqual(
            mocked.call_args_list[1].args[1],
            ["dump", "snapshot", "/meta/platform-metadata.jsonl"],
        )

    def test_verify_targets_the_recorded_latest_successful_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            config["sources"]["claude"]["root"] = str(root / "claude")
            (root / "claude").mkdir()
            (root / "claude" / "settings.json").write_text(
                json.dumps({"cleanupPeriodDays": 3650}),
                encoding="utf-8",
            )
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            conn.execute(
                """
                INSERT INTO captures(
                    capture_id,started_utc,finished_utc,reason,status,
                    restic_snapshot,manifest_sha256
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    "latest-capture",
                    "2026-07-25T20:10:43Z",
                    "2026-07-25T20:11:14Z",
                    "scheduled",
                    "complete",
                    "recorded-snapshot",
                    "recorded-manifest",
                ),
            )
            conn.commit()
            conn.close()
            checked = subprocess.CompletedProcess([], 0, "ok", "")
            sentinel = subprocess.CompletedProcess(
                [],
                0,
                json.dumps(
                    {
                        "capture_id": "latest-capture",
                        "manifest_sha256": "recorded-manifest",
                    }
                ),
                "",
            )
            snapshots = subprocess.CompletedProcess([], 0, "[]", "")
            with mock.patch.object(
                storage,
                "restic",
                side_effect=[checked, sentinel, snapshots],
            ) as mocked:
                result = aa.verify_archive(config)
            self.assertEqual(result["status"], "ok")
            dump_args = mocked.call_args_list[1].args[1]
            self.assertEqual(dump_args[0:2], ["dump", "recorded-snapshot"])
