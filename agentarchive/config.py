"""Default configuration and state-root resolution."""

from __future__ import annotations

import pathlib
import shutil
from typing import Any, Dict, Tuple

from . import common as utils
from .common import DEFAULT_STATE, HOME, KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, LABEL_CAPTURE, LABEL_VERIFY


def default_config(state: pathlib.Path = DEFAULT_STATE) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "state_root": str(state),
        "restic_binary": shutil.which("restic") or "restic",
        "restic_host": "agentarchive",
        "keychain_service": KEYCHAIN_SERVICE,
        "keychain_account": KEYCHAIN_ACCOUNT,
        "launchd_labels": {"capture": LABEL_CAPTURE, "verify": LABEL_VERIFY},
        "local_repository": str(state / "repository"),
        "password_command": (
            f"/usr/bin/security find-generic-password -a {KEYCHAIN_ACCOUNT} "
            f"-s {KEYCHAIN_SERVICE} -w"
        ),
        "capture_schedule": {"Hour": 12, "Minute": 15},
        "require_ac_power": False,
        "verify_require_ac_power": True,
        "skip_low_power_mode": True,
        "hook_coalesce_seconds": 300,
        "excluded_workspaces": [],
        "sources": {
            "claude": {
                "root": str(HOME / ".claude"),
                "paths": [
                    "projects",
                    "file-history",
                    "tasks",
                    "jobs",
                    "plans",
                    "paste-cache",
                    "image-cache",
                    "downloads",
                    "agents",
                    "commands",
                    "skills",
                    "rules",
                    "workflows",
                    "plugins/data",
                    "plugins/marketplaces",
                    "history.jsonl",
                    "stats-cache.json",
                    "settings.json",
                    "CLAUDE.md",
                    "statusline-command.sh",
                    ".claude.json",
                ],
                "excluded": [
                    "cache and plugin installation cache",
                    "debug and telemetry logs",
                    "daemon state and live session locks",
                    "authentication caches and provider credential files",
                    "shell snapshots and environment dumps",
                ],
                "sqlite": [],
                "legacy_cutoff_utc": "",
            },
            "codex": {
                "root": str(HOME / ".codex"),
                "paths": [
                    "sessions",
                    "archived_sessions",
                    "attachments",
                    "memories",
                    "automations",
                    "rules",
                    "skills",
                    "visualizations",
                    "pets",
                    "history.jsonl",
                    "session_index.jsonl",
                    ".codex-global-state.json",
                    ".codex-global-state.json.bak",
                    "AGENTS.md",
                    "config.toml",
                    "config.toml.bak",
                    "version.json",
                    "internal_storage.json",
                ],
                "excluded": [
                    "auth.json and browser credentials",
                    "browser and computer-use runtime profiles",
                    "plugin, model, and vendor caches",
                    "temporary IPC, process-manager, and node runtime state",
                    "shell environment snapshots",
                    "codex logs_2.sqlite (high-churn log database; leaving it uncaptured bounds restic snapshot growth)",
                ],
                "sqlite": [
                    "goals_1.sqlite",
                    "memories_1.sqlite",
                    "queue_1.sqlite",
                    "state_5.sqlite",
                    "thread_history_1.sqlite",
                    "sqlite/codex-dev.db",
                    "sqlite/codex-history-snapshots-dev.db",
                    "sqlite/goals_1.sqlite",
                    "sqlite/memories_1.sqlite",
                    "sqlite/state_5.sqlite",
                ],
                "legacy_cutoff_utc": "",
            },
        },
        "legacy_workspaces": [],
        "legacy_indexes": [],
        "offsite": {
            "enabled": False,
            "repository": "",
            "password_command": "",
            "environment": {},
        },
    }


def load_config(path: pathlib.Path) -> Dict[str, Any]:
    config = utils.read_json(path)
    if not isinstance(config, dict):
        raise RuntimeError(f"missing or invalid configuration: {path}")
    return config


def state_root(config: Dict[str, Any]) -> pathlib.Path:
    return pathlib.Path(config["state_root"]).expanduser().resolve()


def launchd_labels(config: Dict[str, Any]) -> Tuple[str, str]:
    labels = config.get("launchd_labels") or {}
    return labels.get("capture", LABEL_CAPTURE), labels.get("verify", LABEL_VERIFY)
