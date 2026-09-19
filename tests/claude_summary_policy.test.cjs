"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const originalFetch = globalThis.fetch;
const { summaryArgs, withSummaries, guardFetch } = require("../scripts/claude_summary_policy.cjs");
const endpoint = "https://api.anthropic.com/v1/messages?beta=true";
const payload = { model: "claude-example", messages: [{ role: "user", content: "test" }], thinking: { type: "enabled", budget_tokens: 8192, display: "omitted" } };

test("loading in unrelated Node/Bun programs has no side effects", () => {
  assert.equal(globalThis.fetch, originalFetch);
});

test("native Desktop and standalone Claude load the policy", () => {
  const source = fs.readFileSync(path.join(__dirname, "../scripts/claude_summary_policy.cjs"), "utf8");
  for (const execPath of ["/Users/example/Library/Application Support/Claude/claude-code/2.1.275/claude.app/Contents/MacOS/claude", "/Users/example/.local/share/claude/versions/2.1.228"]) {
    const process = { execPath, argv: ["bun", "/$bunfs/root/cli", "--version"], env: {} };
    const context = { process, fetch: originalFetch, URL, Request, Headers, Buffer, module: { exports: {} } };
    vm.runInNewContext(source, context);
    assert.notEqual(context.fetch, originalFetch);
    assert.equal(process.argv[2], "--thinking-display");
    assert.equal(process.argv[3], "summarized");
  }
});

test("startup explicitly enables summaries, preserving literal prompt arguments", () => {
  const prefix = ["bun", "/$bunfs/root/cli"];
  assert.deepEqual(summaryArgs([...prefix, "--thinking-display=omitted", "-p", "hello", "--", "--thinking-display=omitted"]), [...prefix, "--thinking-display", "summarized", "-p", "hello", "--", "--thinking-display=omitted"]);
  assert.deepEqual(summaryArgs([...prefix, "--thinking-display", "omitted", "--version"]), [...prefix, "--thinking-display", "summarized", "--version"]);
});

test("summary policy preserves messages, model, token budget and existing updates", () => {
  const changed = JSON.parse(withSummaries(JSON.stringify(payload)));
  assert.deepEqual(changed, { ...payload, thinking: { ...payload.thinking, display: "summarized" } });
  for (const thinking of [{ type: "disabled" }, { type: "adaptive", display: "updates" }, { type: "adaptive", display: "summarized" }]) {
    const body = JSON.stringify({ ...payload, thinking });
    assert.equal(withSummaries(body), body);
  }
});

test("fetch checks late omission overrides and keeps authentication and cancellation", async () => {
  const calls = [];
  const fetch = guardFetch(async (...args) => { calls.push(args); return new Response("ok"); });
  const signal = new AbortController().signal;
  await fetch(endpoint, { method: "POST", body: JSON.stringify(payload), headers: { "x-api-key": "local-test", "content-length": "1" }, signal });
  assert.equal(JSON.parse(calls[0][1].body).thinking.display, "summarized");
  assert.equal(calls[0][1].headers.get("x-api-key"), "local-test");
  assert.equal(calls[0][1].headers.has("content-length"), false);
  assert.equal(calls[0][1].signal, signal);
  const input = new Request(endpoint, { method: "POST", body: JSON.stringify(payload) });
  await fetch(input);
  assert.equal((await calls[1][0].json()).thinking.display, "summarized");
  assert.equal(input.bodyUsed, false);
  await fetch("https://example.com/v1/messages", { method: "POST", body: "unchanged" });
  assert.equal(calls[2][1].body, "unchanged");
});

test("unsupported compressed inference requests stop before transmission", async () => {
  let called = false;
  const fetch = guardFetch(async () => { called = true; });
  await assert.rejects(fetch(endpoint, { method: "POST", body: "compressed", headers: { "content-encoding": "zstd" } }), /no request sent/);
  assert.equal(called, false);
});
