#!/usr/bin/env python3
"""Install or remove the local native-Claude summary policy, without patching Claude."""

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess

BEGIN = "# BEGIN agent-archive Claude summary policy"
END = "# END agent-archive Claude summary policy"


def configure(remove=False):
    home = pathlib.Path.home()
    root = home / ".claude"
    policy = root / "summary-policy.cjs"
    settings = root / "settings.json"
    zshenv = home / ".zshenv"
    option = "--preload=" + str(policy)
    # Bun parses BUN_OPTIONS itself; this installation uses an unambiguous path.
    if any(c.isspace() for c in str(policy)):
        raise SystemExit("The summary-policy path must not contain whitespace.")
    value = json.loads(settings.read_text()) if settings.exists() else {}
    shell = zshenv.read_text() if zshenv.exists() else ""
    shell = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", shell, flags=re.S)
    previous = subprocess.run(["launchctl", "getenv", "BUN_OPTIONS"], capture_output=True, text=True, check=True).stdout.strip()
    option_pattern = r"(?<!\S)" + re.escape(option) + r"(?!\S)"
    desktop_options = re.sub(option_pattern, "", previous).strip()
    saved_options = value.get("env", {}).get("BUN_OPTIONS", "")
    saved_options = re.sub(option_pattern, "", saved_options).strip()
    backup = root / "summary-policy-backups" / datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup.mkdir(parents=True, mode=0o700)
    for source in (settings, zshenv, policy):
        if source.exists():
            target = backup / source.name
            shutil.copy2(source, target)
            target.chmod(0o600)
    (backup / "launchd-bun-options.json").write_text(json.dumps(previous) + "\n")
    if remove:
        if saved_options:
            value.setdefault("env", {})["BUN_OPTIONS"] = saved_options
        else:
            value.get("env", {}).pop("BUN_OPTIONS", None)
    else:
        source = pathlib.Path(__file__).with_name("claude_summary_policy.cjs")
        shutil.copyfile(source, policy)
        policy.chmod(0o600)
        value["showThinkingSummaries"] = True
        value["alwaysThinkingEnabled"] = True
        value.setdefault("env", {})["BUN_OPTIONS"] = (saved_options + " " + option).strip()
        shell = shell.rstrip() + "\n\n" + BEGIN + "\n" + (
            'case " ${BUN_OPTIONS-} " in\n'
            '  *" ' + option + ' "*) ;;\n'
            '  *) export BUN_OPTIONS="${BUN_OPTIONS:+$BUN_OPTIONS }' + option + '" ;;\n'
            'esac\n'
        ) + END + "\n"
        desktop_options = (desktop_options + " " + option).strip()
    for path, content in ((settings, json.dumps(value, indent=2) + "\n"), (zshenv, shell)):
        temporary = path.with_name(path.name + ".summary-policy-new")
        temporary.write_text(content)
        temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    subprocess.run(["launchctl", "setenv", "BUN_OPTIONS", desktop_options] if desktop_options else ["launchctl", "unsetenv", "BUN_OPTIONS"], check=True)
    # Keep the harmless preload file on removal: already-open shells may reference it.
    if remove:
        policy.write_text('"use strict"; // Summary enforcement removed.\n')
    print(json.dumps({"installed": not remove, "policy": str(policy), "backup": str(backup), "applies_to": "new local native Claude harness processes; restart Desktop when idle"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remove", action="store_true", help="Remove enforcement; retain ordinary thinking preferences.")
    configure(parser.parse_args().remove)
