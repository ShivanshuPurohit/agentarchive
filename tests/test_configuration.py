"""Portable defaults and explicit local identities must coexist on upgrade."""

import pathlib
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

import agent_archive as aa
from agentarchive import common, config, install, reports, storage


class ConfigurationTests(unittest.TestCase):
    def test_new_install_has_no_external_history_or_date_dependencies(self):
        defaults = config.default_config(pathlib.Path("/tmp/archive-test"))
        self.assertEqual(defaults["legacy_indexes"], [])
        self.assertEqual(defaults["legacy_workspaces"], [])
        self.assertTrue(all(not value["legacy_cutoff_utc"] for value in defaults["sources"].values()))

    def test_existing_keychain_snapshot_and_job_identities_are_consumed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            cfg = config.default_config(root / "state")
            cfg.update(
                restic_host="existing-workstation", keychain_service="org.example.existing-key",
                keychain_account="existing-account", password_command="existing-password-command",
                launchd_labels={"capture": "org.example.capture", "verify": "org.example.verify"},
            )
            environment = storage.restic_env(cfg)
            self.assertEqual(environment["RESTIC_HOST"], "existing-workstation")
            self.assertEqual(environment["RESTIC_PASSWORD_COMMAND"], "existing-password-command")
            success = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(common, "run", return_value=success) as run:
                with mock.patch.object(storage.secrets, "token_urlsafe", side_effect=AssertionError("replaced password")):
                    storage.ensure_keychain_password(cfg)
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("-a") + 1], "existing-account")
                self.assertEqual(argv[argv.index("-s") + 1], "org.example.existing-key")
            with mock.patch.object(install, "HOME", root):
                paths = install.launch_plists(cfg, root / "bin/agent-archive", root / "config.json")
                self.assertEqual([p.stem for p in paths], ["org.example.capture", "org.example.verify"])
                for path in paths:
                    self.assertEqual(plistlib.loads(path.read_bytes())["Label"], path.stem)
                with mock.patch.object(common, "run", return_value=success) as run:
                    install.activate_launchd(config=cfg)
                bootstrapped = [call.args[0][-1] for call in run.call_args_list if "bootstrap" in call.args[0]]
                self.assertEqual(bootstrapped, [str(p) for p in paths])

    def test_reports_and_document_index_follow_configured_legacy_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            cfg = config.default_config(root / "state")
            handoff = root / "historical/START_HERE.md"
            handoff.parent.mkdir()
            handoff.write_text("# Historical routing\nuniquelegacydocument\n")
            cfg["legacy_indexes"] = [{"harness": "claude", "start_here": str(handoff)}]
            cfg["legacy_comparison"] = str(root / "comparison.md")
            aa.ensure_private_tree(root / "state")
            conn = aa.connect_catalog(cfg)
            try:
                reports.index_legacy_documents(cfg, conn)
                self.assertEqual(conn.execute("SELECT path FROM documents").fetchone()[0], str(handoff))
                reports.render_start_here(cfg, conn)
            finally:
                conn.close()
            text = (root / "state/START_HERE.md").read_text()
            self.assertIn(str(handoff), text)
            self.assertIn(cfg["legacy_comparison"], text)
            self.assertIn("agent-archive search 'QUERY'", text)
