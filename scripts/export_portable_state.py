#!/usr/bin/env python3
"""Compatibility entrypoint for exporting a portable query runtime."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from agentarchive.portable import SOURCE_ROOT, atomic_json, backup_catalog, copy_tree_if_present, main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"export-portable-state: {exc}", file=sys.stderr)
        raise SystemExit(1)
