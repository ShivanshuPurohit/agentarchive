# Configuration

`agent-archive install` writes the complete configuration to
`~/Library/Application Support/AgentArchive/config.json`. Keep that generated
file local. `--config PATH` selects a different complete configuration file.

## Source and storage settings

| Setting | Purpose |
|---|---|
| `state_root` | Private catalog, indexes, manifests, runtime, and operational state. |
| `sources.claude.root`, `sources.codex.root` | Harness homes; defaults are derived from the current user's home. |
| `sources.HARNESS.paths`, `sources.HARNESS.sqlite` | Retained paths and databases captured through SQLite's backup API. |
| `sources.HARNESS.legacy_cutoff_utc` | Optional cutoff when earlier non-tool content is already covered by a historical index. Empty on new installations. |
| `excluded_workspaces` | Workspace roots omitted from searchable capture and the viewer. |
| `restic_binary` | Executable location, discovered from `PATH` on installation. |
| `local_repository` | Encrypted local recovery repository. |
| `password_command` | Command restic uses to obtain its password. Keep credentials outside the source repository. |
| `restic_host` | Snapshot host identity; defaults to `agentarchive`. |

## Existing installation identities

An existing archive may use custom identifiers. Preserve them in its local
configuration before installing code with different defaults:

```json
{
  "restic_host": "archive-workstation",
  "keychain_service": "org.example.archive.restic",
  "keychain_account": "agentarchive",
  "launchd_labels": {
    "capture": "org.example.archive.capture",
    "verify": "org.example.archive.verify"
  }
}
```

This is a fragment to merge into the generated configuration. Preserve the
existing `password_command`, repository path, sources, and cutoff values.
Changing the Keychain service is not a password migration, and changing a job
label does not remove an older launchd job. `install` and `activate` consume the
configured identities.

## Optional historical indexes

New installations have no historical dependencies. Existing Claude and Codex
evidence databases can be configured through `legacy_indexes`. These adapters
expect AgentArchive's earlier Claude/Codex evidence schemas.

Each entry has `harness`, `name`, `database`, and optionally `start_here`.
Markdown routing documents are indexed from the directory containing
`start_here`. The optional top-level `legacy_comparison` names a comparison
document for the generated entrypoint. `legacy_workspaces` identifies archive
trees used by explicit legacy backup and deduplication commands.

For SSD seeding, also set `workspace` and `seed_paths` on each legacy entry:

```json
{
  "harness": "claude",
  "name": "Earlier Claude history",
  "workspace": "~/Archives/claude",
  "database": "~/Archives/claude/analysis/cutoff/claude_index.sqlite",
  "start_here": "~/Archives/claude/analysis/complete/START_HERE.md",
  "seed_paths": ["source_snapshot/claude_home_example/projects"]
}
```

`seed_paths` are relative to `workspace`. They name the raw evidence directories
to copy; the database and routing document are included separately. The seed
preserves these relative paths and rewrites its reader configuration for WSL.
No particular archive date, username, or volume name is assumed.

`ssd_seed_destination` can hold a local SSD destination so the seed command can
be run without `--destination`. Neither this path nor peer device IDs belong
in the source repository.

## Local files and publication

Generated databases, manifests, credentials, recovered metadata, `.local/`,
and working notes are ignored by Git. Keep machine-specific operational notes
there or under the installed state directory. Repository tests include a guard
against accidentally tracking private state and literal personal home paths.
