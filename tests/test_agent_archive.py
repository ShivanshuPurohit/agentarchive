"""Cli behavior preserved from the original archive test suite."""

import unittest
from unittest import mock
import agent_archive as aa


class ArchiveCliTests(unittest.TestCase):
    def test_backup_command_returns_failure_exit_status(self):
        operation_lock = mock.MagicMock()
        with mock.patch.object(aa, "load_config", return_value={}):
            with mock.patch.object(aa, "exclusive_lock", return_value=operation_lock):
                with mock.patch.object(
                    aa,
                    "backup_archive",
                    return_value={"status": "failed"},
                ):
                    with mock.patch("builtins.print"):
                        status = aa.main(["--config", "/unused", "backup"])
        self.assertEqual(status, 1)
