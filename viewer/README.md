# Rollout viewer

`agent-archive browse` (also `serve`) opens a read-only, localhost viewer of the
existing archive. There is no new transcript database. Discovery uses the archive
catalog and configured historical indexes, including indexed child agents and
retained capture copies. It never recursively discovers live transcript roots.
Run a filtered capture to add newly created rollouts. Opening a known source
reads its retained JSONL, including newer live records.

Tool calls start collapsed. Opening one shows named operations and individually
collapsed results, with exit codes and retained output sizes. Expanding a result
renders its content. The orchestration source and original receipt are separate
disclosures. JavaScript in a recorded tool call is parsed for labels, never run.

The small bottom-right `in · out` footer shows associated **model-call totals**.
Several messages, reasoning blocks, and tool requests can share one call, so
their footers must not be added together. Hover for exact counts, cached and
reasoning tokens, the source line, and how the association was made. Claude uses
message IDs; Codex blocks are associated with the next usage receipt in the same
user turn by recorded event order. User text and tool output have no separate
token attribution unless it was retained; missing values show a dash.

**Turn analysis** groups model usage between human prompts. It prefers Codex
response-ID receipts and deduplicates mirrored cumulative checkpoints. Claude's
repeated content-block usage is counted once per message ID. Counts include
input, cache reads, cache writes, uncached input, output, and thinking/reasoning
where recorded. Unknown values stay unknown. A cumulative-only difference is
labelled because it can cover multiple calls. Child-agent usage stays in that
agent's own source, accessible through Related rollouts.

Codex forks can copy parent history and rewrite its event timestamps. Explicit
response ownership and retained task-start times distinguish inherited usage
from calls made by the child. Inherited receipts stay inspectable but are
excluded from that agent's totals. Ambiguous ownership stays unknown and is
also excluded. These are per-source totals; there is no family-wide usage sum.
Receipt coverage cannot prove that every provider-side operation was retained.

The input chart and output chart have separate, labelled scales. Claude total
input is the sum of its three disjoint input counters; Codex input already
includes cached input. Reasoning is included in output. These rules follow the
[Claude usage documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
and [OpenAI reasoning documentation](https://developers.openai.com/api/docs/guides/reasoning).

**Activity** shows timestamped messages as points and tool request/result or
explicit execution intervals as bars. It covers the loaded transcript portion;
turn usage covers the selected turn in the retained source. Neither view
measures prefill/decode duration, per-token latency, GPU utilization, batching,
or scheduling. Tool-free gaps are unclassified. Receipt timestamps are not
token emission timestamps. These counts describe recorded workload shapes;
they do not establish a kernel bottleneck.

## Presentation and rich content

The viewer uses local React components, platform fonts, and a neutral dark/light
theme. MarkdownIt, Shiki, xterm, KaTeX, Mermaid, PDF.js, Papa Parse, and jsdiff
handle content rendering. Dependency license notices ship with each build.

Supported content includes Markdown tables/lists, highlighted code, edit/patch
diffs, ANSI output and terminal cursor controls, Mermaid and math fences,
HTML/SVG previews, CSV tables, retained images, audio/video controls, and paged
PDF rendering. HTML previews are scriptless sandboxes. Unsupported languages
remain readable plain code. Unsupported/missing files get a reference card;
external media is an explicit link and is never fetched automatically.

Embedded media is loaded lazily through an authenticated source/line/component
coordinate, bounded to 24 MiB per preview. A current local file must be explicitly
referenced, within the source's workspace or adjacent `attachments` directory,
and outside excluded workspaces after symlink resolution. It is labelled as a
current file because it can differ from the historic rollout. There is no
arbitrary-path file endpoint. Text and original JSON remain available on demand.

Dark/light themes and optional CSS glass navigation are available in the header.
Glass is a web effect, not Apple's native Liquid Glass material.

## Build and package

Build from a clean checkout using the locked npm dependencies:

```sh
uv sync --locked
npm --prefix viewer ci
npm --prefix viewer run build
uv run agent-archive browse
uv build
```

The build emits `agentarchive/web`, which is included in wheels, source archives,
installed runtimes, and portable exports. Python serves those packaged assets;
Node is only needed for development. Rebuilding requires restarting the foreground
viewer, which holds a fixed allowlist of that build's assets.

## Validation

```sh
uv run python -m unittest discover -s tests
npm --prefix viewer test
npm --prefix viewer run build
uv run python tests/serve_viewer_fixture.py
```

The last command prints a local URL for a temporary synthetic source through
the production HTTP handler. It exercises code, nested Markdown, diagrams,
math, tables, HTML, ANSI cursor output, diffs, images, PDF, audio, SVG and token
footers. Also verify real Claude/Codex sources, result disclosures, paging,
evidence navigation, themes, and turn selection before installation.

New read-only routes use the existing capability cookie:
`/api/transcripts/{id}/media?line=…&index=…&asset=…` and
`/api/transcripts/{id}/usage?line=…&after=…`. Usage pages contain at most 200
records and the last 500 turn summaries (plus the selected earlier turn).
