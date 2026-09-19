"use strict";

// Loaded by Bun before Claude's bundled entrypoint. No transcript or request logging.
const nativeClaude =
  !process.env.BUN_BE_BUN &&
  process.argv[1]?.startsWith("/$bunfs/") &&
  /\/claude(?:\.app\/Contents\/MacOS\/claude|\/versions\/[^/]+|\/[^/]+\/claude\.app\/Contents\/MacOS\/claude)?$/.test(process.execPath);

function summaryArgs(args) {
  const result = args.slice(0, 2);
  result.push("--thinking-display", "summarized");
  for (let i = 2; i < args.length; i++) {
    if (args[i] === "--") return result.concat(args.slice(i));
    if (args[i] === "--thinking-display") { i++; continue; }
    if (args[i].startsWith("--thinking-display=")) continue;
    result.push(args[i]);
  }
  return result;
}

function withSummaries(body) {
  const value = JSON.parse(body);
  const thinking = value?.thinking;
  if (!Array.isArray(value?.messages) ||
      !["adaptive", "enabled"].includes(thinking?.type) ||
      ["summarized", "updates"].includes(thinking.display)) return body;
  return JSON.stringify({ ...value, thinking: { ...thinking, display: "summarized" } });
}

function guardFetch(original, baseUrl) {
  const origins = new Set(["https://api.anthropic.com"]);
  if (baseUrl) origins.add(new URL(baseUrl).origin);
  const guarded = async function (input, init) {
    const request = input instanceof Request;
    const url = new URL(request ? input.url : String(input));
    const method = (init?.method ?? (request ? input.method : "GET")).toUpperCase();
    if (method !== "POST" || !origins.has(url.origin) || !/\/v1\/messages\/?$/.test(url.pathname)) {
      return original.call(this, input, init);
    }
    const headers = new Headers(init?.headers ?? (request ? input.headers : undefined));
    if (headers.has("content-encoding")) {
      throw new Error("Claude summary policy: compressed request cannot be checked; no request sent.");
    }
    let body = init?.body;
    if (body == null && request) body = await input.clone().text();
    if (ArrayBuffer.isView(body)) body = Buffer.from(body.buffer, body.byteOffset, body.byteLength).toString("utf8");
    if (typeof body !== "string") {
      throw new Error("Claude summary policy: unsupported request body; no request sent.");
    }
    const rewritten = withSummaries(body);
    if (rewritten === body) return original.call(this, input, init);
    headers.delete("content-length");
    if (request) return original.call(this, new Request(input, { ...init, body: rewritten, headers }));
    return original.call(this, input, { ...init, body: rewritten, headers });
  };
  return Object.assign(guarded, original);
}

if (nativeClaude) {
  process.argv = summaryArgs(process.argv);
  globalThis.fetch = guardFetch(globalThis.fetch, process.env.ANTHROPIC_BASE_URL);
}

module.exports = { summaryArgs, withSummaries, guardFetch };
