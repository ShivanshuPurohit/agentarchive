# AgentArchive

AgentArchive continuously preserves local Claude Code and Codex work without relying on either client’s retention policy.

## Quick start

The capture service targets macOS with APFS, Python 3.9 or newer, and `restic`
available on `PATH`. The application uses Python's standard library. The
portable reader also runs inside WSL.

With Claude Code and Codex initialized on the machine, run:

```bash
python3 scripts/import_claude_ui.py /path/to/refreshes/2026-09-17
npm --prefix viewer ci
npm --prefix viewer run build
python3 agent_archive.py install --activate
"$HOME/.local/bin/agent-archive" backup
"$HOME/.local/bin/agent-archive" browse
```

Installation creates private configuration under
`~/Library/Application Support/AgentArchive/config.json`. Edit that file for
source locations, workspace exclusions, historical indexes, or custom service
identities. Fresh installations scan retained history without a date cutoff.
See [CONFIGURATION.md](CONFIGURATION.md) for configuration and upgrade details.

## Code layout

`agent_archive.py` is the command entrypoint and a compatibility facade for
existing Python consumers. The `agentarchive/` package contains the implementation:

- `indexing.py`, `search.py`, `context.py`: extract, find, and expand evidence.
- `catalog.py`, `reports.py`: schema, health records, capsules, and onboarding.
- `capture.py`, `staging.py`, `storage.py`: capture orchestration, inventories,
  SQLite backups, and restic snapshots.
- `audit.py`, `recovery.py`, `metadata.py`: preserved-file accounting and verified
  staging recovery, including macOS metadata.
- `install.py`, `schedule.py`, `runtime.py`: installation, scheduled operations,
  and the shared runtime inventory used by Mac, portable export, and SSD seeding.
- `replica.py`, `offsite.py`, `dedup.py`: replica verification and storage maintenance.
- `common.py`, `config.py`: shared primitives and configuration.

The existing standalone reasoning index, privacy filter, and transcript viewer
remain separate. Tests are grouped by behavior; run them with
`python3 -m unittest discover -s tests`.

It has four deliberately separate layers:

1. APFS copy-on-write staging and consistent SQLite backups.
2. Encrypted, chunk-deduplicated restic snapshots containing raw evidence and append-only manifests.
3. A rebuildable SQLite catalog, incremental FTS index, and normalized readable-reasoning index.
4. Deterministic Markdown capsules and a cross-harness `START_HERE.md` for agent onboarding.

The catalog, capsules, and onboarding Markdown are normal plaintext files readable by Claude and Codex under the user's macOS account. Encryption applies only to the restic recovery repositories, not to day-to-day indexing or search.

Authentication files, browser credentials, caches, and temporary runtimes are excluded. Conversation files, externalized tool results, attachments, memories, tasks, custom instructions, useful configuration, and consistent Codex state databases are retained.

That sentence describes the normal searchable capture. The cross-device setup
uses a separate plaintext, device-namespaced Google Drive backup for complete
homes; it never merges Mac machine state into WSL. See
[CROSS_DEVICE.md](CROSS_DEVICE.md) for the exact Google Drive, Syncthing, WSL,
SSD-seeding, privacy, and recovery contract.

`excluded_workspaces` in `config.json` is a hard transcript boundary shared by
Claude and Codex. A session whose recorded working directory is that path or a
descendant is removed from capture staging before indexing or restic backup,
rejected by Claude's checkpoint hook, omitted from exact-session lookup, and
hidden from the live transcript viewer. Adding a root also purges any matching
derived catalog rows and capsules. Live files under `~/.claude` and `~/.codex`
are never changed; their retention remains the user's decision.

## Installed locations

- Program and private state: `~/Library/Application Support/AgentArchive/`
- CLI symlink: `~/.local/bin/agent-archive`
- Daily job, including while on battery: `~/Library/LaunchAgents/org.agentarchive.capture.plist`
- Weekly AC-power-only verification and restore test: `~/Library/LaunchAgents/org.agentarchive.verify.plist`
- Claude `SessionEnd` hook: merged into `~/.claude/settings.json`

The restic password lives in the macOS login Keychain under service
`org.agentarchive.restic-local` by default. Existing installations can retain a
custom service name in private configuration. The password is not stored in
this project, either harness home, or the restic repository.

The service does not wake the Mac. Claude's `SessionEnd` hook makes a fast APFS copy-on-write checkpoint of only the just-ended transcript and returns. Consolidation, hashing, SQLite backups, indexing, and restic work run once per day on either AC or battery power, while still deferring in Low Power Mode. The heavier weekly full-repository verification keeps its own AC-power requirement. The launchd jobs run with low CPU priority and low-priority I/O.

## Commands

```bash
agent-archive status
agent-archive backup
agent-archive replica-manifest
agent-archive replica-verify /path/to/raw-replica-manifest.json.gz
agent-archive capture --reason manual
agent-archive search 'timeout AND retry'
agent-archive reasoning 'rejected architecture'
agent-archive browse
agent-archive serve
agent-archive serve --port 0 --no-open
agent-archive context 'i:codex:BLOCK_ID' --max-chars 4000
agent-archive context 'r:codex:PAYLOAD_ID' --max-chars 4000
agent-archive context 'l:claude:BLOCK_ID' --max-chars 4000
agent-archive context '/absolute/raw/path.jsonl:LINE' --max-chars 4000
agent-archive verify --read-data
agent-archive dedup-report
agent-archive staging-audit
agent-archive staging-cleanup
```

`agent-archive backup` is the normal manual workflow: it captures both harnesses,
writes the encrypted Restic snapshot, checks the catalog and repository, and
restores the recorded sentinel. Repository checks automatically run Restic's
stale-only `unlock` and retry once after lock exit code 11; active locks remain
locked and make the command fail.

Manual backup validates the consistent catalog snapshot before storing it and
runs the repository check once during final verification. Its result labels
`verify.catalog_basis` as `checked capture snapshot`; standalone `verify` checks
the current live catalog. This avoids two full catalog scans and two repository
checks in one backup without omitting the captured-catalog integrity check or
sentinel restore.

Search is deliberately two-stage and token-bounded. The first command returns
at most eight deduplicated 18-token snippets by default, prefers all-signal
matches, and uses broad OR retrieval only when the strict query has no result.
Its text view emits stable hit IDs plus compact session coordinates; selected
`context` output and `search --json` retain full absolute provenance. Use
`context` only on a selected hit ID or coordinate; it reads indexed blocks,
defaults to a 6,000-character total budget, and marks truncation. Increase that
budget only after the terse result has been chosen.

Tool-call inputs are indexed alongside their outputs, including Codex freeform
`custom_tool_call.input`, conventional `function_call.arguments`, older flat
Codex records, and Claude `tool_use.input`. The next normal `capture` or `backup`
after upgrading to 1.6.0 revisits previously scanned transcripts once, including
unchanged files, and adds missing calls. Tool calls are checked even before the
historical date cutoff. This uses retained, privacy-filtered source files; it
cannot recreate a transcript absent from those sources.

The per-file `indexed_files.tool_calls_version` checkpoint records that backfill.
Existing block IDs and output rows are retained. New evidence and upgrade
checkpoints commit together; a failed index transaction can be retried without
partial checkpoints or duplicate calls. Subsequent captures return to incremental
scanning. Capture results report `indexing.files_backfilled` and
`indexing.tool_calls_added`; calls can be retrieved with the existing `search`
and `context` commands.

SQLite FTS5 is the retrieval backend. At the present catalog size it is not the
bottleneck; relevance ranking, duplicate collapse, and bounded expansion are.
Semantic embeddings should be added only if a checked golden-query suite shows
lexical recall failures that exact plus short-synonym queries cannot recover.

`reasoning` searches Claude plaintext `thinking` blocks plus the readable Codex
reasoning surfaces that the ordinary message extractor previously missed:
`reasoning.summary[].summary_text`, plaintext reasoning content, and
`event_msg/agent_reasoning`. Codex `encrypted_content` is never represented as
recovered text; only its presence and byte count are recorded while the raw
encrypted value remains in the authoritative transcript snapshot.

Reasoning is normalized rather than copied into another transcript database.
Each exact text payload is stored and tokenized once, while small occurrence
rows retain every session, source file, raw line, timestamp, and capture
coordinate. Search returns terse, deduplicated hit cards; `context` expands one
selected payload and a bounded list of its raw occurrences. Full chain-of-thought
is not injected into an agent prompt by default because hypotheses are often
repetitive or superseded and must be checked against tool output and final
artifacts.

The experimental `agent_browse.db` is not an archival source. Its useful
reasoning/search/view behavior is now provided without a second content store.
`agent_browse.py` remains for migration compatibility; rebuilding its separate
database requires an explicit duplicate-data acknowledgement flag.

`browse` (also `serve`) starts a foreground, read-only viewer on a random `127.0.0.1` port and
normally opens it in the browser. It never becomes a launchd job and stops on
Ctrl-C. The existing catalogs provide global discovery and evidence search;
the selected transcript is paged directly from its authoritative JSONL, so a
live appended record can appear before the next capture without creating a
second content database. Normal, Thinking, Verbose, and Summary are view
filters over one normalized event stream. Large item bodies load in bounded
chunks, tool calls join to results by file-local call ID, and opaque reasoning
is labeled rather than presented as recovered text.

The viewer uses locally imported Claude presentation assets, collapsed readable
tool results, rich code/terminal/diff/media rendering, small per-call token
footers, and a turn analysis view for recorded input/cache/output/reasoning
counts. See [viewer build, usage semantics, and validation](viewer/README.md).
Building from source requires the pinned local capture; installed and portable
runtimes already include the build and do not require Node.

The persistent sidebar automatically loads every canonical top-level Claude
and Codex chat, grouped by project; subagent and workflow rollouts are an
explicit opt-in. Session-registry rescans run in the background, so opening or
filtering the sidebar never waits on a multi-second live-root refresh, and they
run at most once every five minutes while the viewer is being used. Claude
`isMeta` injections and Codex lifecycle envelopes appear only in Verbose,
while their raw bytes remain available there and in the authoritative JSONL.

The viewer binds only to numeric localhost, uses a fresh per-launch capability
cookie, accepts no mutations, exposes no arbitrary path route, and renders all
transcript text as untrusted content. `--no-open` prints the one-time bootstrap
URL for manual use. Search freshness remains capture-bounded and is labeled
separately from live raw-transcript freshness.

`dedup-apply` never removes required paths. It verifies exact SHA-256 identity and replaces duplicate workspace files with APFS copy-on-write clones. Both paths continue to work, and later edits diverge safely.

`staging-audit` hashes every file and symlink in an abandoned capture workspace,
records directories and extended-attribute hashes, and compares the result only
against catalog entries whose restic snapshots still exist. The detailed,
compressed audit is retained under `audits/staging/`.

`staging-cleanup` is a dry run unless `--apply` is supplied. Applied cleanup
creates a manifest and recovery sentinel, backs up the complete staging tree,
restores and validates the sentinel, confirms the tree did not change during
backup, and runs `restic check` before an atomic quarantine-and-delete. Use
`staging-cleanup --apply --read-data` for a one-time full read of every
repository blob before deletion. If any step fails, the staging tree remains in
place; a later run resumes from the already-verified recovery snapshot instead
of uploading it again. Recovered file inventories and proof reports stay in the
catalog, `manifests/`, and the recovery snapshot.

## Authority and recovery

Raw restic snapshots and their `manifest.jsonl.gz` files are authoritative. SQLite and Markdown are derived and can be regenerated. Every capture records additions, changes, unchanged files, and tombstones for deletions.

Because restic on macOS does not itself retain every Apple-specific metadata
field, each new snapshot also carries a hash-bound
`meta/platform-metadata.jsonl` sidecar. It records birth times, file flags,
hard-link coordinates, and raw extended-attribute values (base64 plus SHA-256)
for files, symlinks, and directories. Snapshot verification restores and hashes
this sidecar in addition to the ordinary recovery sentinel.

Restore a snapshot into a separate directory:

```bash
agent-archive restore latest /tmp/agentarchive-restore
```

Do not restore directly over live `~/.claude` or `~/.codex` without inspecting the recovered tree first.

## Off-device backup

The local repository is fully encrypted, but a second repository still needs an external volume or remote object-store destination:

`configure-offsite` is transactional and migrates all historical snapshots by
default. It initializes a fresh destination with compatible chunker parameters,
proves the copied inventory, runs a repository check, restores the latest
sentinel, and only then enables future offsite captures. An interrupted copy is
safe to rerun. For an external disk:

```bash
agent-archive configure-offsite /Volumes/EncryptedBackup/agentarchive-restic
```

`configure-offsite` creates another encrypted restic repository. An optional
plaintext Google Drive workflow is provided by `scripts/plain_cloud_backup.py`.
Syncthing can carry rollout and consistent-catalog updates between macOS and
WSL. These transports have different privacy and recovery properties; see
[CROSS_DEVICE.md](CROSS_DEVICE.md).
