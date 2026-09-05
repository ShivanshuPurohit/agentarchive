#!/usr/bin/env bash
set -euo pipefail

seed_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
seed_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["seed_id"])' "$seed_dir/manifest.json")"
imports="$HOME/.local/share/AgentArchiveImports"
target="$imports/$seed_id"
staging="$imports/.${seed_id}.partial"

if [[ -e "$target" || -e "$staging" ]]; then
  echo "Refusing to overwrite an existing import: $target" >&2
  exit 1
fi

cd "$seed_dir"
sha256sum -c SHA256SUMS
mkdir -p "$imports" "$HOME/.local/bin"
mkdir "$staging"
mkdir "$staging/tools"
cp -p "$seed_dir/tools/configure_syncthing.py" "$staging/tools/configure_syncthing.py"
cp -p "$seed_dir/tools/privacy_filter.py" "$staging/tools/privacy_filter.py"
tar -xf payload/current-raw-mac.tar -C "$staging"
for update in "$seed_dir"/payload/raw-update-*.tar; do
  [[ -e "$update" ]] || continue
  tar -xf "$update" -C "$staging"
done
tar -xf payload/agentarchive-portable.tar -C "$staging"
tar -xf payload/historical-evidence.tar -C "$staging"
mv "$staging/portable/config.wsl-readonly.json" "$staging/config.wsl-readonly.json"
mv "$staging/portable/WSL_START_HERE.md" "$staging/WSL_START_HERE.md"
python3 - "$staging" <<'PY'
import json, pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
config = json.loads((root / "config.wsl-readonly.json").read_text())
import_root = pathlib.Path("~/.local/share/AgentArchiveImports/current")
databases = [pathlib.Path("portable/catalog.sqlite")]
databases.extend(pathlib.Path(entry["database"]).relative_to(import_root)
                 for entry in config.get("legacy_indexes", []))
for relative in databases:
    connection = sqlite3.connect(f"file:{root / relative}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()
    if not result or result[0] != "ok":
        raise SystemExit(f"SQLite verification failed for {relative}: {result!r}")
PY

python3 - "$staging/raw" "$HOME" "$staging/conflicts" "$staging/merge-receipt.json" <<'PY'
import hashlib, json, os, pathlib, shutil, sys

raw, home, conflicts, receipt_path = map(pathlib.Path, sys.argv[1:])
mappings = (
    (raw / "claude/projects", home / ".claude/projects", "claude-projects"),
    (raw / "claude/file-history", home / ".claude/file-history", "claude-file-history"),
    (raw / "codex/sessions", home / ".codex/sessions", "codex-sessions"),
    (raw / "codex/archived_sessions", home / ".codex/archived_sessions", "codex-archived-sessions"),
    (raw / "codex/attachments", home / ".codex/attachments", "codex-attachments"),
)
counts = {"installed": 0, "identical": 0, "conflicts": 0, "symlinks": 0}

def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.digest()

def same(source, target):
    if source.is_symlink() or target.is_symlink():
        return source.is_symlink() and target.is_symlink() and os.readlink(source) == os.readlink(target)
    if not source.is_file() or not target.is_file():
        return False
    return source.stat().st_size == target.stat().st_size and digest(source) == digest(target)

for source_root, target_root, label in mappings:
    target_root.mkdir(parents=True, exist_ok=True)
    if not source_root.is_dir():
        continue
    entries = sorted(
        (path for path in source_root.rglob("*") if path.is_file() or path.is_symlink()),
        key=lambda path: path.relative_to(source_root).as_posix(),
    )
    for source in entries:
        relative = source.relative_to(source_root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(target):
            shutil.move(str(source), str(target))
            counts["installed"] += 1
            counts["symlinks"] += int(target.is_symlink())
            continue
        if same(source, target):
            source.unlink()
            counts["identical"] += 1
            continue
        retained = conflicts / label / relative
        retained.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(retained))
        counts["conflicts"] += 1

receipt_path.write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
print(json.dumps(counts, indent=2))
PY

mv "$staging" "$target"
ln -sfn "$target" "$imports/.current.new"
mv -Tf "$imports/.current.new" "$imports/current"

wrapper="$HOME/.local/bin/agent-archive"
if [[ -e "$wrapper" || -L "$wrapper" ]]; then
  cp -p "$wrapper" "$wrapper.pre-wsl-seed-$seed_id"
fi
cat >"$wrapper" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
root="$HOME/.local/share/AgentArchiveImports/current/portable"
config="$HOME/.local/share/AgentArchiveImports/current/config.wsl-readonly.json"
exec python3 "$root/bin/agent-archive" --config "$config" "$@"
SH
chmod 700 "$wrapper"

instruction_block='<!-- agentarchive-cross-device:start -->
# Cross-device AgentArchive continuity
When a request may continue earlier work from Claude or Codex, first run
`$HOME/.local/bin/agent-archive search '\''QUERY'\''`. Use the returned hit ID
with `agent-archive context HIT_ID --max-chars 6000` when exact tool calls or
terminal output matter. Read
`$HOME/.local/share/AgentArchiveImports/current/WSL_START_HERE.md` for routing.
Search narrowly; do not ingest the entire archive.
<!-- agentarchive-cross-device:end -->'

append_instruction() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  if [[ -f "$path" ]] && grep -q 'agentarchive-cross-device:start' "$path"; then
    return
  fi
  if [[ -f "$path" ]]; then
    cp -p "$path" "$path.pre-agentarchive-$seed_id"
  fi
  printf '\n%s\n' "$instruction_block" >>"$path"
}

append_instruction "$HOME/.claude/CLAUDE.md"
append_instruction "$HOME/.codex/AGENTS.md"

"$wrapper" search 'session' --limit 2 >/dev/null
echo "Installed AgentArchive as $wrapper"
echo "Raw history was merged without overwriting conflicts; auth, settings, and mutable state were untouched."
echo "When Syncthing is installed, get this device ID with: syncthing device-id"
