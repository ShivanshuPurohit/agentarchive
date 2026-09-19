"""Public archives must reject private runtime data and personal file coordinates."""
import pathlib
import tempfile
import unittest
import zipfile

from scripts.check_distribution import check


class DistributionPrivacyTests(unittest.TestCase):
    def test_private_records_and_personal_paths_are_rejected(self):
        cases = [
            (".local/transcript.jsonl", b"{}"),
            ("catalog.sqlite", b"database"),
            ("example.py", str(pathlib.Path("/Users") / "fixture-owner" / "private").encode()),
            ("../outside.txt", b"escape"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "fixture.whl"
            for name, data in cases:
                with self.subTest(name=name):
                    with zipfile.ZipFile(target, "w") as archive:
                        archive.writestr(name, data)
                    with self.assertRaisesRegex(ValueError, "Private|Personal"):
                        check(target)

    def test_wheel_without_viewer_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory) / "fixture.whl"
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("agent_archive.py", b"# CLI only")
            with self.assertRaisesRegex(ValueError, "Incomplete distribution"):
                check(target)
