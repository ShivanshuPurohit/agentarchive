"""Installed and portable CLIs must work without the source checkout on sys.path."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import agent_archive as aa
from agentarchive import install, runtime, storage


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ArchiveRuntimeTests(unittest.TestCase):
    def run_cli(self, entrypoint, config, cwd):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(entrypoint), "--config", str(config),
             "search", "absent-test-evidence", "--json"],
            cwd=cwd, env=env, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), [])

    def config(self, root):
        config = aa.default_config(root / "state")
        config["legacy_indexes"] = []
        for harness, definition in config["sources"].items():
            definition["root"] = str(root / "live" / harness)
            pathlib.Path(definition["root"]).mkdir(parents=True)
        aa.write_json(root / "live/claude/settings.json", {})
        aa.ensure_private_tree(root / "state")
        aa.connect_catalog(config).close()
        path = root / "state/config.json"
        aa.write_json(path, config)
        return path

    def test_install_and_repackage_keep_the_complete_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = self.config(root)
            with (
                mock.patch.object(install, "HOME", root / "home"),
                mock.patch.object(storage, "ensure_keychain_password"),
                mock.patch.object(storage, "ensure_repository"),
            ):
                result = install.install(config)
            self.run_cli(pathlib.Path(result["installed"]), config, root)
            runtime.copy_runtime(root / "state/bin", root / "repackaged")
            self.run_cli(root / "repackaged/agent-archive", config, root)
            # Restored code includes the same package needed by the installed CLI.
            stage = root / "stage"
            cfg = aa.load_config(config)
            for definition in cfg["sources"].values():
                definition.update(paths=[], sqlite=[])
            aa.stage_sources(cfg, stage)
            self.assertTrue((stage / "data/agentarchive/agentarchive/indexing.py").is_file())
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            subprocess.run(
                [sys.executable, "-c", "import transcript_viewer"],
                cwd=stage / "data/agentarchive", env=env,
                capture_output=True, text=True, check=True,
            )

    def test_portable_export_copies_a_runnable_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            config = self.config(root)
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/export_portable_state.py"),
                 "--state-root", str(root / "state"), "--target", str(root / "portable"),
                 "--source-root", str(ROOT)],
                cwd=root, capture_output=True, text=True, check=True,
            )
            self.run_cli(root / "portable/bin/agent-archive", config, root)
