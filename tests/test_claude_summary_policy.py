"""Exercise install/remove against isolated user files, without touching launchd."""
import contextlib
import io
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_claude_summaries import BEGIN, configure


class SummaryPolicyInstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_remove_preserves_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            home = pathlib.Path(directory)
            (home / ".claude").mkdir()
            settings = home / ".claude/settings.json"
            shell = home / ".zshenv"
            existing = '--preload="/example/path with spaces/other.cjs"'
            settings.write_text(json.dumps({"env": {"BUN_OPTIONS": existing, "KEEP": "yes"}, "hooks": {"SessionEnd": []}}))
            shell.write_text('export KEEP="yes"\n')
            launch = {"value": existing}

            def command(args, **kwargs):
                if args[1] == "getenv":
                    return subprocess.CompletedProcess(args, 0, launch["value"], "")
                launch["value"] = args[3] if args[1] == "setenv" else ""
                return subprocess.CompletedProcess(args, 0, "", "")

            with patch("pathlib.Path.home", return_value=home), patch("scripts.configure_claude_summaries.subprocess.run", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
                configure()
                first = settings.read_text(), shell.read_text()
                configure()
                self.assertEqual(first, (settings.read_text(), shell.read_text()))
                self.assertEqual(shell.read_text().count(BEGIN), 1)
                value = json.loads(settings.read_text())
                self.assertTrue(value["showThinkingSummaries"])
                self.assertTrue(value["alwaysThinkingEnabled"])
                self.assertIn(existing, value["env"]["BUN_OPTIONS"])
                self.assertEqual(value["env"]["KEEP"], "yes")
                self.assertEqual(value["hooks"], {"SessionEnd": []})
                configure(remove=True)
                self.assertEqual(shell.read_text().strip(), 'export KEEP="yes"')
                self.assertEqual(json.loads(settings.read_text())["env"]["BUN_OPTIONS"], existing)
                self.assertEqual(launch["value"], existing)
                self.assertIn("enforcement removed", (home / ".claude/summary-policy.cjs").read_text())
