import test from "node:test";
import assert from "node:assert/strict";
import {
  activity,
  jsonSequence,
  textBlocks,
  toolModel,
  resultReceipts,
} from "./model.js";
import { scriptOperations } from "./operations.js";

const request = {
  phase: "request",
  text_preview: '{"cmd":"make test"}',
  timestamp: "2026-01-01T00:00:00Z",
};
const result = (text, timestamp = "2026-01-01T00:00:05Z") => ({
  phase: "result",
  text_preview: text,
  timestamp,
});
const tool = (...components) => ({
  kind: "tool",
  tool_name: "exec_command",
  components: [request, ...components],
});

test("receipts distinguish yielded work, failure and a returned result", () => {
  assert.equal(toolModel(tool()).status, "unresolved");
  assert.equal(
    toolModel(
      tool(result('{"session_id":42,"exit_code":null,"output":"Working"}')),
    ).status,
    "yielded",
  );
  assert.equal(
    toolModel(tool(result("Script running with cell ID 42"))).status,
    "yielded",
  );
  assert.equal(
    toolModel(tool(result("Process exited with code 2"))).status,
    "error",
  );
  assert.equal(
    toolModel(tool(result('{"exit_code":1,"output":"failed"}'))).status,
    "error",
  );
  assert.equal(toolModel(tool(result("done"))).status, "done");
});
test("execution timestamps supersede request/receipt intervals without inventing clocks", () => {
  assert.equal(
    toolModel(tool(result("done"))).timing.basis,
    "recorded call interval",
  );
  const execution = toolModel(
    tool(
      { phase: "start", timestamp: "2026-01-01T00:00:02Z" },
      result("done"),
      {
        phase: "lifecycle",
        timestamp: "2026-01-01T00:00:04Z",
        status: "success",
      },
    ),
  );
  assert.equal(execution.timing.to - execution.timing.from, 2000);
  assert.equal(execution.timing.basis, "execution");
  assert.equal(toolModel(tool(result("done", "not a timestamp"))).timing, null);
  assert.equal(toolModel(tool(result("done", "2025-01-01"))).timing, null);
});
test("activity preserves overlapping tools and point-only model events", () => {
  const events = activity([
    { ...tool(result("done")), line: 3 },
    { ...tool(result("done")), line: 5 },
    {
      kind: "message",
      role: "assistant",
      timestamp: "2026-01-01T00:00:03Z",
      line: 4,
    },
    { kind: "message", mirror: true, timestamp: "2026-01-01T00:00:03Z" },
    { kind: "reasoning", text_preview: "no clock" },
  ]);
  assert.equal(events.length, 3);
  assert.equal(events[2].point, true);
  assert.equal(events[2].from, events[2].to);
  assert.equal(events[0].from, events[1].from);
});
test("nested orchestration output becomes content, including adjacent JSON values", () => {
  const output =
    '{"status":"fulfilled","value":{"output":"ok\\n","exit_code":0}} [{"content":[{"type":"text","text":"# Report"}]}]';
  assert.deepEqual(textBlocks(output), [
    { type: "stdout", text: "ok\n" },
    { type: "text", text: "# Report" },
  ]);
  assert.deepEqual(jsonSequence('{"text":"a } \\" b"}'), [{ text: 'a } " b' }]);
  assert.equal(jsonSequence('{"broken":'), null);
  assert.equal(jsonSequence("ordinary prose {x}"), null);
  assert.deepEqual(
    textBlocks('Wall time: 1 second\nOutput:\n{"stdout":"ok"}'),
    [{ type: "stdout", text: "ok" }],
  );
});
test("media bytes are rendered through source references, never dumped into fields", () => {
  assert.deepEqual(textBlocks({ type: "image", data: "binary" }), []);
  assert.deepEqual(textBlocks({ stderr: "failed", stdout: "ok" }), [
    { type: "stdout", text: "ok" },
    { type: "stderr", text: "failed" },
  ]);
});
test("the reported wall-of-JSON case becomes individually expandable receipts", () => {
  const text =
    "Script completed Wall time 0.1 seconds Output: " +
    '{"i":0,"status":"fulfilled","value":{"exit_code":1,"output":"working directory\\n"}} ' +
    '{"i":1,"status":"fulfilled","value":{"exit_code":0,"output":"# Start here\\n\\nReadable **text**"}}';
  const receipts = resultReceipts(text);
  assert.equal(receipts.length, 2);
  assert.equal(receipts[0].exit, 1);
  assert.equal(receipts[0].status, "error");
  assert.equal(receipts[1].index, 1);
  assert.equal(receipts[1].output, "# Start here\n\nReadable **text**");
});
test("operation labels inspect code without executing or inventing dynamic values", () => {
  const operations = scriptOperations(
    'await Promise.all([tools.exec_command({cmd:"pwd"}),tools.exec_command({cmd:"cat /project/START_HERE.md"}),tools.exec_command({cmd:secret})]);',
  );
  assert.equal(operations.length, 3);
  assert.equal(operations[0].title, "pwd");
  assert.equal(operations[1].title, "Read START_HERE.md");
  assert.equal(operations[2].command, undefined);
  assert.deepEqual(scriptOperations("unfinished ( "), []);
});
test("activity does not call injected instructions model work and retains unresolved requests", () => {
  const timestamp = "2026-01-01T00:00:00Z";
  const events = activity([
    { kind: "message", role: "developer", timestamp },
    { kind: "message", role: "user", visibility: "verbose", timestamp },
    { ...tool(), line: 3 },
  ]);
  assert.equal(events.length, 1);
  assert.equal(events[0].lane, "Tools");
  assert.equal(events[0].point, true);
  assert.equal(events[0].status, "unresolved");
});
