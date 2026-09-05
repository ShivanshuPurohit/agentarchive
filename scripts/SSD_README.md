# AgentArchive Mac seed for WSL

This is a **copy**, not a move. The Mac sources remain untouched. The payload is
plaintext and contains the portable AgentArchive catalog plus raw Claude/Codex
rollout evidence. It merges only the five history subdirectories into WSL's
normal `~/.claude` and `~/.codex` trees. It does not replace those directories,
and it never imports auth, settings, caches, installation identity, or mutable
state databases.

From inside WSL, find the SSD below `/mnt` and run:

```bash
bash /mnt/DRIVE/agent-archive/mac-seed-*/install_wsl_seed.sh
```

The installer verifies every tar SHA-256 and every SQLite database before it
activates the import. Existing imports are never overwritten. It installs the
query command at:

```bash
~/.local/bin/agent-archive search 'timeout AND retry'
```

Existing rollout files are compared byte-for-byte. Identical files are reused;
different files are retained under the import's `conflicts/` tree and never
overwritten. The installer also appends a bounded continuity instruction to
`~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, backing up either file first.
That instruction is what makes a new agent consult the archive when you ask it
to remember earlier work.

The initial SSD seed is intentionally uncompressed: this keeps CPU use low and
preserves symlinks that exFAT cannot store directly. Later synchronization
should be incremental; do not rebuild this seed for every update.

Raw update tars are overlays applied after the original raw seed. The portable
catalog tar is replaced atomically when a newer verified catalog is published,
so refreshing this seed does not duplicate its unchanged historical evidence.

The WSL-only query config and routing note live outside the receive-only catalog
folder, so later Mac catalog publications cannot overwrite or remove them.

For the live incremental phase, install Syncthing inside WSL and run
`syncthing device-id`. The seed installs the privacy-aware pairing helper under
`~/.local/share/AgentArchiveImports/current/tools/configure_syncthing.py`; send
the WSL device ID back to the Mac before applying either peer's configuration.
