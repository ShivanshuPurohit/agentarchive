"""Workspace privacy boundary shared by AgentArchive capture and viewing."""

from __future__ import annotations

import contextlib
import json
import pathlib
from typing import Any, Dict, Iterator


def excluded_roots(config: Dict[str, Any]) -> tuple[pathlib.Path, ...]:
    roots = []
    for value in config.get("excluded_workspaces", []):
        if isinstance(value, str) and value.strip():
            roots.append(pathlib.Path(value).expanduser().resolve(strict=False))
    return tuple(roots)


def workspace_is_excluded(config: Dict[str, Any], value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = pathlib.Path(value).expanduser().resolve(strict=False)
    return any(candidate == root or root in candidate.parents for root in excluded_roots(config))


def _workspace_values(event: Dict[str, Any]) -> Iterator[str]:
    containers = [event]
    payload = event.get("payload")
    if isinstance(payload, dict):
        containers.append(payload)
        settings = payload.get("thread_settings")
        if isinstance(settings, dict):
            containers.append(settings)
    for container in containers:
        for key in ("cwd", "project"):
            value = container.get(key)
            if isinstance(value, str):
                yield value
        for key in ("workspace_roots", "workspaces"):
            values = container.get(key)
            if isinstance(values, list):
                yield from (value for value in values if isinstance(value, str))


def transcript_is_excluded(
    config: Dict[str, Any], path: pathlib.Path, ceiling: int = 512 * 1024
) -> bool:
    """Inspect only bounded workspace metadata, never message text."""
    if not excluded_roots(config):
        return False
    consumed = 0
    try:
        with path.open("rb") as handle:
            while consumed < ceiling:
                raw = handle.readline(min(ceiling - consumed, 512 * 1024) + 1)
                if not raw or consumed + len(raw) > ceiling:
                    break
                consumed += len(raw)
                with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
                    event = json.loads(raw)
                    if isinstance(event, dict) and any(
                        workspace_is_excluded(config, value)
                        for value in _workspace_values(event)
                    ):
                        return True
    except OSError:
        return False
    return False
