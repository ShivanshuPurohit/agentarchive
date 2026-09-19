"""Installation of the CLI, runtime, hooks, and launchd config."""

from __future__ import annotations

import os
import pathlib
import plistlib
import shlex
import shutil
from typing import Any, Dict, List, Optional

from . import catalog, common as utils, config as configuration, indexing, reports, runtime, storage
from .common import HOME
from .common import DEFAULT_SOURCE_ROOT


def merge_claude_hook(config: Dict[str, Any], installed: pathlib.Path, config_path: pathlib.Path) -> None:
    settings = pathlib.Path(config["sources"]["claude"]["root"]) / "settings.json"
    value = utils.read_json(settings)
    if not isinstance(value, dict):
        raise RuntimeError(f"Claude settings are not valid JSON: {settings}")
    cleanup = value.get("cleanupPeriodDays")
    if not isinstance(cleanup, int) or cleanup < 3650:
        value["cleanupPeriodDays"] = 3650
    hooks = value.setdefault("hooks", {})
    session_end = hooks.setdefault("SessionEnd", [])
    command = f"{shlex.quote(str(installed))} --config {shlex.quote(str(config_path))} hook"
    already = False
    for matcher in session_end:
        if not isinstance(matcher, dict):
            continue
        for hook in matcher.get("hooks", []):
            if isinstance(hook, dict) and "agent-archive" in str(hook.get("command", "")):
                hook.update({"type": "command", "command": command, "timeout": 5})
                already = True
    if not already:
        session_end.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": 5,
                    }
                ]
            }
        )
    backup = configuration.state_root(config) / "settings-backups" / f"claude-settings-{utils.capture_id()}.json"
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(settings, backup)
    os.chmod(backup, 0o600)
    utils.write_json(settings, value, 0o600)


def launch_plists(config: Dict[str, Any], installed: pathlib.Path, config_path: pathlib.Path) -> List[pathlib.Path]:
    state = configuration.state_root(config)
    capture_label, verify_label = configuration.launchd_labels(config)
    launch_dir = HOME / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    common = {
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "ThrottleInterval": 60,
    }
    capture_plist = {
        **common,
        "Label": capture_label,
        "ProgramArguments": [str(installed), "--config", str(config_path), "run-scheduled"],
        "StartCalendarInterval": config.get("capture_schedule") or {"Hour": 12, "Minute": 15},
        "StandardOutPath": str(state / "logs" / "capture.stdout.log"),
        "StandardErrorPath": str(state / "logs" / "capture.stderr.log"),
    }
    verify_plist = {
        **common,
        "Label": verify_label,
        "ProgramArguments": [str(installed), "--config", str(config_path), "run-verify-scheduled"],
        "StartCalendarInterval": {"Weekday": 0, "Hour": 13, "Minute": 15},
        "StandardOutPath": str(state / "logs" / "verify.stdout.log"),
        "StandardErrorPath": str(state / "logs" / "verify.stderr.log"),
    }
    paths = [
        launch_dir / f"{capture_label}.plist",
        launch_dir / f"{verify_label}.plist",
    ]
    for path, value in zip(paths, (capture_plist, verify_plist)):
        utils.atomic_write(path, plistlib.dumps(value, fmt=plistlib.FMT_XML), 0o600)
    return paths


def install(config_path: pathlib.Path, activate: bool = False) -> Dict[str, Any]:
    if config_path.exists():
        config = configuration.load_config(config_path)
        # Migrate the short-lived hourly prototype without overwriting any
        # deliberate laptop-safe choices the user has already made.
        defaults = configuration.default_config(config_path.parent)
        for key in (
            "capture_schedule",
            "require_ac_power",
            "verify_require_ac_power",
            "skip_low_power_mode",
            "hook_coalesce_seconds",
            "excluded_workspaces",
        ):
            config.setdefault(key, defaults[key])
        claude_paths = config.setdefault("sources", {}).setdefault("claude", {}).setdefault("paths", [])
        if "CLAUDE.md" not in claude_paths:
            claude_paths.append("CLAUDE.md")
        for harness, definition in defaults["sources"].items():
            configured = config.setdefault("sources", {}).setdefault(harness, {})
            configured_sqlite = configured.setdefault("sqlite", [])
            for relative in definition.get("sqlite", []):
                if relative not in configured_sqlite:
                    configured_sqlite.append(relative)
        config.pop("capture_interval_seconds", None)
        utils.write_json(config_path, config)
    else:
        config = configuration.default_config(config_path.parent)
        utils.ensure_private_tree(config_path.parent)
        utils.write_json(config_path, config)
    state = configuration.state_root(config)
    utils.ensure_private_tree(state)
    config["config_path"] = str(config_path)
    utils.write_json(config_path, config)
    installed = state / "bin" / "agent-archive"
    source_root = DEFAULT_SOURCE_ROOT
    runtime.copy_runtime(source_root, state / "bin")
    portable_exporter = state / "bin" / "export-portable-state"
    portable_source = source_root / "agentarchive" / "portable.py"
    if not portable_source.is_file():
        portable_source = source_root / "scripts" / "export_portable_state.py"
    if not portable_source.is_file():
        portable_source = source_root / "export-portable-state"
    if portable_source.resolve() != portable_exporter.resolve():
        shutil.copy2(portable_source, portable_exporter)
    os.chmod(portable_exporter, 0o700)
    source_readme = source_root / "README.md"
    if source_readme.is_file():
        shutil.copy2(source_readme, state / "README.md")
        os.chmod(state / "README.md", 0o600)
    local_link = HOME / ".local" / "bin" / "agent-archive"
    local_link.parent.mkdir(parents=True, exist_ok=True)
    if local_link.exists() or local_link.is_symlink():
        local_link.unlink()
    local_link.symlink_to(installed)
    storage.ensure_keychain_password(config)
    storage.ensure_repository(config)
    conn = catalog.connect_catalog(config)
    purged_sessions = indexing.purge_excluded_sessions(config, conn)
    if purged_sessions:
        reports.render_start_here(config, conn)
    conn.close()
    merge_claude_hook(config, installed, config_path)
    plists = launch_plists(config, installed, config_path)
    result = {
        "installed": str(installed),
        "config": str(config_path),
        "repository": config["local_repository"],
        "launch_agents": [str(path) for path in plists],
        "activated": False,
    }
    if activate:
        activate_launchd(plists)
        result["activated"] = True
    return result


def activate_launchd(paths: Optional[List[pathlib.Path]] = None, config: Optional[Dict[str, Any]] = None) -> None:
    if paths is None:
        capture_label, verify_label = configuration.launchd_labels(config or {})
        paths = [
            HOME / "Library" / "LaunchAgents" / f"{capture_label}.plist",
            HOME / "Library" / "LaunchAgents" / f"{verify_label}.plist",
        ]
    domain = f"gui/{os.getuid()}"
    for path in paths:
        label = path.stem
        utils.run(["/bin/launchctl", "bootout", f"{domain}/{label}"], check=False)
        result = utils.run(["/bin/launchctl", "bootstrap", domain, str(path)], check=False)
        if result.returncode != 0 and "already" not in (result.stderr or "").lower():
            raise RuntimeError(f"launchctl bootstrap failed for {path}: {result.stderr}")
