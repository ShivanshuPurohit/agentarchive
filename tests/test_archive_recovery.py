"""Recovery behavior preserved from the original archive test suite."""

import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock
import agent_archive as aa
from agentarchive import recovery, storage


class ArchiveRecoveryTests(unittest.TestCase):
    def test_incomplete_stage_audit_requires_a_snapshot_that_still_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            conn.execute(
                """
                INSERT INTO captures(
                    capture_id,started_utc,finished_utc,reason,status,restic_snapshot
                ) VALUES('good','2026-07-20T00:00:00Z','2026-07-20T00:01:00Z',
                         'test','complete','snapshot-present')
                """
            )
            archived = root / "archived.txt"
            archived.write_text("already safe", encoding="utf-8")
            digest = aa.sha256_file(archived)
            conn.execute(
                """
                INSERT INTO files(
                    capture_id,harness,relpath,file_type,size_bytes,mtime_ns,mode,
                    uid,gid,sha256,link_target,change_kind
                ) VALUES('good','codex','same.txt','file',12,1,420,0,0,?,NULL,'new')
                """,
                (digest,),
            )
            conn.commit()
            stage = root / "state" / "staging" / "failed"
            (stage / "data" / "codex" / "empty").mkdir(parents=True)
            (stage / "data" / "codex" / "same.txt").write_text(
                "already safe",
                encoding="utf-8",
            )
            (stage / "data" / "codex" / "unique.txt").write_text(
                "only here",
                encoding="utf-8",
            )
            by_path, by_content = aa.archived_file_indexes(
                conn,
                {"snapshot-present"},
            )
            audit = aa.audit_incomplete_stage(stage, by_path, by_content)
            self.assertEqual(audit["counts"]["same_path_archived"], 1)
            self.assertEqual(audit["counts"]["not_in_durable_snapshot"], 1)
            self.assertEqual(audit["counts"]["empty_directories"], 1)
            absent_path, absent_content = aa.archived_file_indexes(
                conn,
                {"some-other-snapshot"},
            )
            absent = aa.audit_incomplete_stage(stage, absent_path, absent_content)
            self.assertEqual(absent["counts"]["not_in_durable_snapshot"], 2)
            conn.close()

    def test_recovery_verifies_repository_before_deleting_incomplete_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            stage_id = "20260722T064648.443078Z"
            conn.execute(
                """
                INSERT INTO captures(capture_id,started_utc,finished_utc,reason,status,error)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    stage_id,
                    "2026-07-22T06:46:48Z",
                    "2026-07-22T06:47:18Z",
                    "scheduled",
                    "failed",
                    "permission denied",
                ),
            )
            conn.commit()
            stage = root / "state" / "staging" / stage_id
            (stage / "data" / "codex").mkdir(parents=True)
            (stage / "data" / "codex" / "unique.txt").write_text(
                "treasure",
                encoding="utf-8",
            )
            checked = subprocess.CompletedProcess([], 0, "no errors were found", "")
            snapshots = subprocess.CompletedProcess([], 0, "[]", "")
            with mock.patch.object(
                storage,
                "known_restic_snapshot_ids",
                return_value=set(),
            ), mock.patch.object(
                storage,
                "snapshot_for_capture",
                return_value="",
            ), mock.patch.object(
                storage,
                "backup_stage",
                return_value="recovered-snapshot",
            ), mock.patch.object(
                storage,
                "verify_snapshot_sentinel",
                return_value=(True, "ok"),
            ), mock.patch.object(
                storage,
                "restic",
                side_effect=[checked, snapshots],
            ) as mocked_restic:
                result = aa.recover_incomplete_stages(
                    config,
                    conn,
                    read_data=True,
                )
            self.assertFalse(stage.exists())
            self.assertEqual(result["deleted_stages"], [stage_id])
            self.assertEqual(result["repository_check"], "read-data")
            row = conn.execute(
                "SELECT status,restic_snapshot,file_count FROM captures WHERE capture_id=?",
                (stage_id,),
            ).fetchone()
            self.assertEqual(row["status"], "recovered_orphan")
            self.assertEqual(row["restic_snapshot"], "recovered-snapshot")
            self.assertEqual(row["file_count"], 1)
            self.assertEqual(
                mocked_restic.call_args_list[0].args[1],
                ["check", "--read-data"],
            )
            conn.close()

    def test_recovery_includes_failed_stage_with_valid_existing_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            stage_id = "failed-stage-with-manifest"
            stage = root / "state" / "staging" / stage_id
            (stage / "data" / "codex").mkdir(parents=True)
            (stage / "data" / "codex" / "unique.txt").write_text(
                "treasure", encoding="utf-8"
            )
            meta = stage / "meta"
            meta.mkdir()
            manifest_data = b"original manifest bytes\n"
            manifest_digest = aa.hashlib.sha256(manifest_data).hexdigest()
            (meta / "manifest.jsonl.gz").write_bytes(manifest_data)
            platform_data = b'{"path":"retained"}\n'
            platform_digest = aa.hashlib.sha256(platform_data).hexdigest()
            (meta / "platform-metadata.jsonl").write_bytes(platform_data)
            aa.write_json(
                meta / "restore-sentinel.json",
                {
                    "capture_id": stage_id,
                    "manifest_sha256": manifest_digest,
                    "platform_metadata_path": "platform-metadata.jsonl",
                    "platform_metadata_sha256": platform_digest,
                },
            )
            conn.execute(
                """
                INSERT INTO captures(
                    capture_id,started_utc,finished_utc,reason,status,
                    manifest_sha256,error
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    stage_id,
                    "2026-08-30T09:34:18Z",
                    "2026-08-30T09:48:00Z",
                    "manual",
                    "failed",
                    manifest_digest,
                    "interrupted",
                ),
            )
            conn.commit()

            def backup(*_args, **_kwargs):
                self.assertEqual((meta / "manifest.jsonl.gz").read_bytes(), manifest_data)
                self.assertTrue((meta / "orphan-stage-audit.json.gz").is_file())
                return "recovered-snapshot"

            checked = subprocess.CompletedProcess([], 0, "no errors were found", "")
            snapshots = subprocess.CompletedProcess([], 0, "[]", "")
            with mock.patch.object(
                storage, "known_restic_snapshot_ids", return_value=set()
            ), mock.patch.object(
                storage, "snapshot_for_capture", return_value=""
            ), mock.patch.object(
                storage, "backup_stage", side_effect=backup
            ), mock.patch.object(
                storage, "verify_snapshot_sentinel", return_value=(True, "ok")
            ), mock.patch.object(
                recovery, "write_orphan_stage_metadata"
            ) as rewritten, mock.patch.object(
                storage, "restic", side_effect=[checked, snapshots]
            ):
                result = aa.recover_incomplete_stages(config, conn, read_data=True)
            rewritten.assert_not_called()
            self.assertEqual(result["deleted_stages"], [stage_id])
            self.assertFalse(stage.exists())
            conn.close()

    def test_recovery_keeps_stage_when_repository_check_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = aa.default_config(root / "state")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(config)
            stage_id = "failed-stage"
            conn.execute(
                """
                INSERT INTO captures(capture_id,started_utc,reason,status)
                VALUES(?,?,?,'failed')
                """,
                (stage_id, "2026-07-22T06:46:48Z", "test"),
            )
            conn.commit()
            stage = root / "state" / "staging" / stage_id
            (stage / "data" / "claude").mkdir(parents=True)
            (stage / "data" / "claude" / "unique.jsonl").write_text(
                '{"valuable":true}\n',
                encoding="utf-8",
            )
            failed_check = subprocess.CompletedProcess([], 1, "", "pack is damaged")
            with mock.patch.object(
                storage,
                "known_restic_snapshot_ids",
                return_value=set(),
            ), mock.patch.object(
                storage,
                "snapshot_for_capture",
                return_value="",
            ), mock.patch.object(
                storage,
                "backup_stage",
                return_value="recovered-snapshot",
            ), mock.patch.object(
                storage,
                "verify_snapshot_sentinel",
                return_value=(True, "ok"),
            ), mock.patch.object(
                storage,
                "restic",
                return_value=failed_check,
            ):
                with self.assertRaisesRegex(RuntimeError, "deletion was refused"):
                    aa.recover_incomplete_stages(config, conn)
            self.assertTrue(stage.exists())
            row = conn.execute(
                "SELECT status,restic_snapshot FROM captures WHERE capture_id=?",
                (stage_id,),
            ).fetchone()
            self.assertEqual(row["status"], "orphan_recovery_pending_cleanup")
            self.assertEqual(row["restic_snapshot"], "recovered-snapshot")
            checked = subprocess.CompletedProcess([], 0, "no errors were found", "")
            snapshots = subprocess.CompletedProcess([], 0, "[]", "")
            with mock.patch.object(
                storage,
                "known_restic_snapshot_ids",
                return_value={"recovered-snapshot"},
            ), mock.patch.object(
                storage,
                "backup_stage",
            ) as mocked_backup, mock.patch.object(
                storage,
                "verify_snapshot_sentinel",
                return_value=(True, "ok"),
            ), mock.patch.object(
                storage,
                "restic",
                side_effect=[checked, snapshots],
            ):
                retried = aa.recover_incomplete_stages(config, conn)
            mocked_backup.assert_not_called()
            self.assertEqual(retried["deleted_stages"], [stage_id])
            self.assertFalse(stage.exists())
            final = conn.execute(
                "SELECT status FROM captures WHERE capture_id=?",
                (stage_id,),
            ).fetchone()
            self.assertEqual(final["status"], "recovered_orphan")
            conn.close()
