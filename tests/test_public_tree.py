"""Keep private runtime data and literal personal home paths out of Git."""

import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PublicTreeTests(unittest.TestCase):
    def test_tracked_tree_contains_code_and_synthetic_examples_only(self):
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True)
        if result.returncode:
            self.skipTest("Source archive has no Git index")
        private_roots = {".local", ".zcode", "metadata-supplements", "repository", "indexes", "manifests", "audits"}
        personal_home = re.compile(rb"/(?:Users|home)/(?!example(?:/|\b))[-\w.]+")
        credential = re.compile(rb"\b(?:ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{32,})\b")
        device_id = re.compile(rb"\b(?:[A-Z2-7]{7}-){7}[A-Z2-7]{7}\b")
        for name in result.stdout.decode().split("\0"):
            if not name:
                continue
            relative = pathlib.Path(name)
            with self.subTest(path=name):
                self.assertNotIn(relative.parts[0], private_roots)
                self.assertNotIn(name, {"WORK_LOG.md", "config.json", "config.local.json", ".env"})
                self.assertNotIn(relative.suffix, {".sqlite", ".db", ".jsonl"})
                data = (ROOT / relative).read_bytes()
                self.assertIsNone(personal_home.search(data), "literal personal home path")
                self.assertIsNone(credential.search(data), "credential-shaped value")
                self.assertIsNone(device_id.search(data), "device identity")
