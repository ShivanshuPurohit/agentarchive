# Keep Claude thinking summaries enabled

Run `python3 scripts/configure_claude_summaries.py` on this Mac to install the
summary policy. Restart Claude Desktop when existing work is idle; existing
harness processes cannot acquire a startup preload retroactively.

The installer keeps `showThinkingSummaries` and `alwaysThinkingEnabled` enabled,
installs `~/.claude/summary-policy.cjs`, and supplies its Bun preload through
Claude settings, the user's `.zshenv`, and the current login's launchd environment.
Desktop derives its harness environment from the login shell. The shell setup
persists across logins and app updates; it does not depend on a versioned binary
path. Existing settings and shell files are backed up inside
`~/.claude/summary-policy-backups/` before changes.

The preload activates only inside a native Claude executable. It supplies
`--thinking-display summarized` and corrects missing/omitted display on enabled
or adaptive `/v1/messages` requests before transmission. It preserves summaries
already requested via `summarized` or `updates`, thinking budgets, tools, prompts,
authentication, and abort signals. It does not log request contents, credentials,
session identities, or incognito-workspace metadata. It does not decrypt thinking.

This is a local workaround, not an Anthropic-supported always-on setting. Its
consumer is Claude's native startup and request path. It supersedes manually
re-enabling summaries and permits subagents to inherit an explicit display
setting. Retire it when Claude provides a verified setting that prevents both
subagent omission and Desktop's later display overrides.

Scope: native Claude Code and local Code sessions in Claude Desktop on this Mac.
It does not install into remote SSH hosts, Cowork VMs, browser chats, npm/Node
Claude distributions, or already-running harness processes. Requests with
thinking explicitly disabled are left alone: summary display cannot create
reasoning that a helper/model did not perform. The provider can still return
empty or redacted blocks. Claude's separate replay filter can still hide text
that remains in the JSONL log.

If a future native runtime introduces compressed inference bodies, the policy
stops those requests with a clear error instead of silently losing enforcement.
Validate after runtime changes with `node --test tests/claude_summary_policy.test.cjs`.
The implementation was also exercised using native Claude 2.1.275 against a
local HTTP mock; no paid inference request was used for that validation.

To remove enforcement, run `python3 scripts/configure_claude_summaries.py --remove`
and restart Claude when idle. This retains the ordinary thinking preferences and
leaves an inert preload file so existing shell environments remain usable.
