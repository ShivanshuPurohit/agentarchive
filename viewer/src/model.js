// Archive adapter. Views consume typed presentations; source references remain intact.
import {
  commandTitle,
  operationSummary,
  scriptOperations,
} from "./operations.js";
export const parse = (text) => {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
};
export const millis = (value) => {
  const n = Date.parse(value);
  return Number.isFinite(n) ? n : null;
};
export const elapsed = (ms) =>
  ms < 1000
    ? `${Math.round(ms)}ms`
    : ms < 60000
      ? `${(ms / 1000).toFixed(1)}s`
      : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
export const displayValue = (value) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2);
// Orchestration receipts can contain multiple adjacent JSON documents.
export function jsonSequence(text) {
  const values = [];
  let start = -1,
    depth = 0,
    quoted = false,
    escaped = false,
    end = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (start < 0) {
      if (!/\s/.test(c)) {
        if (c !== "{" && c !== "[") return null;
        start = i;
        depth = 1;
      }
      continue;
    }
    if (quoted) {
      if (escaped) escaped = false;
      else if (c === "\\") escaped = true;
      else if (c === '"') quoted = false;
      continue;
    }
    if (c === '"') quoted = true;
    else if (c === "{" || c === "[") depth++;
    else if (c === "}" || c === "]") {
      if (--depth === 0) {
        const value = parse(text.slice(start, i + 1));
        if (value === null) return null;
        values.push(value);
        start = -1;
        end = i + 1;
      }
    }
  }
  return values.length && !text.slice(end).trim() ? values : null;
}
export function receiptStatus(text) {
  const decoded = parse(text);
  if (decoded && typeof decoded === "object") {
    if (
      decoded.isError ||
      decoded.is_error ||
      (Number.isInteger(decoded.exit_code) && decoded.exit_code !== 0)
    )
      return "error";
    if (decoded.session_id != null && decoded.exit_code == null)
      return "yielded";
  }
  const exit = /Process exited with code\s+(-?\d+)/i.exec(text);
  if ((exit && Number(exit[1]) !== 0) || /Script failed/i.test(text))
    return "error";
  if (
    /(?:Script running with cell ID|Process running with session ID)/i.test(
      text,
    )
  )
    return "yielded";
  return "done";
}
export function toolModel(item, parts = item.components || []) {
  const request = parts.find((p) => p.phase === "request") || {};
  const input = parse(request.text_preview) ?? request.text_preview ?? "";
  const name = item.tool_name || request.tool_name || "Tool";
  const key = name.toLowerCase();
  const command = input?.cmd ?? input?.command;
  const isScript =
    /(?:^|__)exec$|functions\.exec/.test(key) && typeof input === "string";
  const operations = isScript ? scriptOperations(input) : [];
  const kind = isScript
    ? "script"
    : command != null || /bash|exec_command|write_stdin/.test(key)
      ? "shell"
      : /edit|apply_patch/.test(key)
        ? "diff"
        : /read|view_image/.test(key)
          ? "read"
          : /search|grep|glob/.test(key)
            ? "search"
            : /spawn|agent|task/.test(key)
              ? "agent"
              : "generic";
  const results = parts.filter((p) => !["request", "start"].includes(p.phase));
  const output = results
    .filter((p) => p.phase === "result")
    .map((p) => p.text_preview)
    .join("\n");
  const lastResult = results.filter((p) => p.phase === "result").at(-1);
  const status =
    item.status === "error"
      ? "error"
      : lastResult
        ? receiptStatus(lastResult.text_preview || "")
        : results.some((p) => p.phase === "lifecycle" && p.status)
          ? "done"
          : "unresolved";
  const start = parts.find((p) => p.phase === "start");
  const end =
    [...parts].reverse().find((p) => p.phase === "lifecycle") || lastResult;
  const from = millis((start || request).timestamp),
    to = millis(end?.timestamp);
  const timing =
    from != null && to != null && to >= from
      ? { from, to, basis: start ? "execution" : "recorded call interval" }
      : null;
  return {
    name,
    input,
    request,
    results,
    output,
    kind,
    status,
    timing,
    operations,
    title: command
      ? commandTitle(command)
      : isScript
        ? operationSummary(operations)
        : input?.file_path || input?.path || input?.query || item.title || name,
    code: isScript ? input : command,
    cwd: input?.cwd || input?.workdir,
  };
}
export function resultReceipts(text) {
  // Some harnesses persist the envelope on one line, others retain newlines.
  const envelope =
    /^(?:Chunk ID:|Wall time:|Script completed|Script running|Script failed)[\s\S]*?\b(?:Output|Final output):\s*([\s\S]*)$/i.exec(
      text,
    );
  const body = envelope ? envelope[1] : text;
  const decoded = jsonSequence(body);
  const values = decoded || [body];
  return values.map((value, index) => {
    const wrapped =
      value && typeof value === "object" && value.status === "fulfilled";
    const payload = wrapped ? value.value : value;
    const metadata = payload && typeof payload === "object" ? payload : {};
    const output = metadata.output ?? metadata.content ?? payload;
    return {
      index: wrapped && Number.isInteger(value.i) ? value.i : null,
      payload,
      output,
      exit: metadata.exit_code,
      seconds: metadata.wall_time_seconds,
      status: receiptStatus(displayValue(payload)),
      key: index,
    };
  });
}
export function activity(items) {
  const events = [];
  for (const item of items) {
    if (item.mirror || item.duplicate) continue;
    if (item.kind === "tool") {
      const tool = toolModel(item);
      if (tool.timing)
        events.push({
          ...tool.timing,
          lane: "Tools",
          label: tool.title,
          line: item.line,
          status: tool.status,
          point: false,
        });
      else if (millis(tool.request.timestamp) != null)
        events.push({
          from: millis(tool.request.timestamp),
          to: millis(tool.request.timestamp),
          lane: "Tools",
          label: tool.title,
          line: item.line,
          status: tool.status,
          point: true,
          basis: "request timestamp; completion unknown",
        });
    } else if (["message", "reasoning"].includes(item.kind)) {
      if (
        item.kind === "message" &&
        (!["assistant", "user"].includes(item.role) ||
          item.visibility === "verbose")
      )
        continue;
      const time = millis(item.timestamp);
      if (time != null)
        events.push({
          from: time,
          to: time,
          lane: item.role === "user" ? "User" : "Model",
          label:
            item.kind === "reasoning"
              ? "Recorded thinking"
              : `${item.role} message`,
          line: item.line,
          point: true,
        });
    }
  }
  return events;
}
export function textBlocks(value) {
  if (value == null) return [];
  if (typeof value === "string") {
    const decoded = jsonSequence(value);
    if (decoded) return decoded.flatMap(textBlocks);
    const receipt =
      /^(?:Chunk ID:|Wall time:|Script completed|Script running|Script failed)[\s\S]*?\b(?:Output|Final output):\s*([\s\S]*)$/.exec(
        value,
      );
    return receipt ? textBlocks(receipt[1]) : [{ type: "text", text: value }];
  }
  if (Array.isArray(value)) return value.flatMap(textBlocks);
  if (
    [
      "image",
      "input_image",
      "output_image",
      "audio",
      "video",
      "document",
      "resource",
      "resource_link",
    ].includes(value.type)
  )
    return [];
  if (value.type === "text" && typeof value.text === "string")
    return textBlocks(value.text);
  if (value.status === "fulfilled" && value.value != null)
    return textBlocks(value.value);
  if (
    value.output != null &&
    ("exit_code" in value ||
      "wall_time_seconds" in value ||
      "session_id" in value)
  )
    return [{ type: "stdout", text: String(value.output) }];
  if (value.output != null) return textBlocks(value.output);
  if (value.content != null) return textBlocks(value.content);
  if (typeof value.stdout === "string" || typeof value.stderr === "string")
    return [
      ...(value.stdout ? [{ type: "stdout", text: value.stdout }] : []),
      ...(value.stderr ? [{ type: "stderr", text: value.stderr }] : []),
    ];
  return [{ type: "fields", value }];
}
