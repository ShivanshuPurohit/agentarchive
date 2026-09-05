# AgentArchive across macOS and WSL

The capture service owns one writable catalog. Other devices receive consistent
query snapshots and retained transcript files; they do not merge SQLite state.

| Flow | Data | Behavior |
|---|---|---|
| Raw history replica | Claude projects/file history and Codex sessions/attachments | Syncthing transfers changes between the harness homes. |
| Portable query state | Consistent catalog, manifests, indexes, and reader runtime | The capture host publishes; the receiving device reads. |
| Recovery backup | Encrypted restic snapshots, or an explicitly chosen plaintext cloud backup | Kept separately from the live query surface. |

Do not resume the same session UUID on two devices concurrently. Conflicting
files are retained for inspection, not silently treated as another conversation.

## Export a query snapshot

```bash
python3 scripts/export_portable_state.py
```

The default destination is `~/.local/share/AgentArchivePortable`. The exporter
uses SQLite's backup API and replaces the published catalog atomically. Sync
that directory with a Send Only folder on the producer and Receive Only on the
consumer. The live catalog's WAL and shared-memory files are not transport data.

On WSL, use the received reader with its portable configuration:

```bash
python3 ~/.local/share/AgentArchivePortable/bin/agent-archive \
  --config ~/.local/share/AgentArchivePortable/config.portable.json \
  search 'QUERY'
```

Raw transcript paths resolve through the receiving device's own `~/.claude`
and `~/.codex` directories. Authentication files, live application databases,
settings, and installation identities are not merged into those directories.

## Seed an SSD

On the capture host, choose a destination explicitly or set
`ssd_seed_destination` in the local configuration:

```bash
python3 scripts/create_ssd_seed.py --destination /Volumes/ARCHIVE/agent-archive
```

The seed contains checksummed tar files, a consistent portable catalog, and a
WSL installer. Configured historical archives require explicit `seed_paths`;
see [CONFIGURATION.md](CONFIGURATION.md). With no legacy indexes configured,
the seed contains only the current history and portable query state.

Inside WSL, run the installer from the selected seed directory:

```bash
bash /mnt/DRIVE/agent-archive/mac-seed-EXAMPLE/install_wsl_seed.sh
```

The installer verifies the tar checksums and configured databases before
activation. Existing identical files are reused; differing files go into the
import's conflicts directory. It installs the reader wrapper and adds bounded
archive-lookup instructions to both harnesses' local instruction files, with
backups of the previous files. See [the SSD guide](scripts/SSD_README.md).

## Configure synchronization

Run Syncthing inside the same WSL environment used by Claude and Codex. Obtain
that device's ID with `syncthing device-id`, then pass the actual peer ID to
`scripts/configure_syncthing.py`; use `--help` for its source/receiver options.
The helper previews changes by default. `--apply` writes the configuration and
preserves its previous state for recovery.

Device IDs, remote names, and local source locations are machine configuration.
Keep them outside the source repository. The helper derives privacy exclusions
from the selected AgentArchive configuration before sharing the raw folders.

## Optional plaintext cloud recovery

`scripts/plain_cloud_backup.py` uses an already configured Google Drive rclone
remote and separate device namespaces. It copies changed files, retains replaced
versions, and does not propagate source deletions. It excludes the rebuildable
portable catalog from this cloud flow.

This optional complete-home backup has a wider scope than normal searchable
capture: harness homes can contain credentials and excluded-workspace history.
The command requires explicit acknowledgement of both before even preparing
its operations. Inspect `--help` and the planned sources before using `--apply`.
The encrypted alternative is `agent-archive configure-offsite`.

## Recovery boundary

A Mac application database is not a replacement for a WSL application's live
state. Restore backups into a separate directory and inspect them first. Keep
raw evidence, query snapshots, and machine-specific application state distinct.
