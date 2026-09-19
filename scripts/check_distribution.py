#!/usr/bin/env python3
"""Check built source archives and wheels for required assets and private files."""
import argparse
import pathlib
import re
import tarfile
import zipfile

PRIVATE_PARTS = {".git", ".local", ".venv", "node_modules", "__pycache__", "source_snapshot", "staging", "repository"}
PERSONAL_PATH = re.compile(rb"/(?:Users|home)/(?!example(?:/|\b))[-\w.]+")
TOKEN = re.compile(rb"\b(?:ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{32,})\b")


def members(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    yield name, archive.read(name)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.isfile():
                    yield member.name, archive.extractfile(member).read()


def check(path):
    names = []
    for name, data in members(path):
        names.append(name)
        parts = pathlib.PurePosixPath(name).parts
        if pathlib.PurePosixPath(name).is_absolute() or ".." in parts or PRIVATE_PARTS.intersection(parts) or pathlib.PurePosixPath(name).suffix in {".sqlite", ".db", ".jsonl"}:
            raise ValueError(f"Private/runtime file in distribution: {name}")
        if name.endswith((".py", ".js", ".jsx", ".css", ".html", ".md", ".toml", ".txt", "METADATA", "PKG-INFO")):
            if PERSONAL_PATH.search(data) or TOKEN.search(data):
                raise ValueError(f"Personal path or credential-shaped value in distribution: {name}")
    for required in ("agent_archive.py", "agentarchive/portable.py", "agentarchive/web/index.html", "agentarchive/web/THIRD_PARTY_NOTICES.txt"):
        if not any(name == required or name.endswith("/" + required) for name in names):
            raise ValueError(f"Incomplete distribution; missing {required}")
    print(f"{path.name}: {len(names)} files; required runtime present; privacy checks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=pathlib.Path)
    for path in parser.parse_args().archives:
        check(path)
