# Development

The capture service targets macOS; the portable query runtime also supports
Linux/WSL. Python 3.9+ and SQLite with FTS5 are required. Backup operations use
Restic and the macOS Keychain. Building the viewer requires Node.js 22.12+.

```sh
uv sync --locked
npm --prefix viewer ci
npm --prefix viewer run build
uv run python -m unittest discover -s tests
npm --prefix viewer test
uv run python tests/serve_viewer_fixture.py
```

The last command serves temporary synthetic transcripts through the actual
viewer handler. Check both themes, collapsed tool results, code, terminal output,
diagrams, media, and turn analysis. Use synthetic data for screenshots and tests.
Keep changes focused; archive preservation and existing CLI contracts take
precedence over cosmetic refactoring.

## Local distribution checks

```sh
uv build
uv run python scripts/check_distribution.py dist/*
```

Builds include the viewer and its generated dependency license notices. Runtime
data, settings, caches, Git metadata, and local notes must stay outside source
archives and wheels. Paths belong in user configuration, not source defaults.

The package is not published to PyPI. Its `Private :: Do Not Upload` classifier
blocks accidental PyPI uploads. Before a public release, choose a project
license and review the intended release archive. A source archive does not
include Git history; making an existing repository public also exposes deleted
files and old commit metadata, which requires a separate history review.
