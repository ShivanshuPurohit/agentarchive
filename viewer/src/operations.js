import { parse as parseScript } from "acorn";

// Inspect recorded JavaScript without evaluating it. Expressions stay expressions.
export function scriptOperations(script) {
  let ast;
  try {
    ast = parseScript(script, {
      ecmaVersion: "latest",
      sourceType: "module",
      allowAwaitOutsideFunction: true,
    });
  } catch {
    return [];
  }
  const calls = [];
  function literal(node) {
    if (node?.type === "Literal") return node.value;
    if (node?.type === "TemplateLiteral" && !node.expressions.length)
      return node.quasis[0].value.cooked;
    return undefined;
  }
  function walk(node) {
    if (!node || typeof node !== "object") return;
    if (
      node.type === "CallExpression" &&
      node.callee.type === "MemberExpression" &&
      node.callee.object.name === "tools"
    ) {
      const name = node.callee.property.name || literal(node.callee.property);
      const argument = node.arguments[0],
        values = {};
      for (const prop of argument?.properties || []) {
        const key = prop.key?.name || literal(prop.key);
        values[key] = literal(prop.value);
      }
      const command = values.cmd || values.command;
      calls.push({
        name,
        command,
        cwd: values.workdir || values.cwd,
        path: values.path || values.file_path,
        title: command
          ? commandTitle(command)
          : values.path ||
            values.file_path ||
            values.query ||
            name?.replace(/^mcp__|__/g, " ").replace(/_/g, " "),
        source: script.slice(node.start, node.end),
        input: literal(argument),
        offset: node.start,
      });
    }
    for (const value of Object.values(node))
      if (Array.isArray(value)) value.forEach(walk);
      else if (value && typeof value === "object") walk(value);
  }
  walk(ast);
  return calls.sort((a, b) => a.offset - b.offset).slice(0, 100);
}

export function commandTitle(command) {
  const first = String(command).trim().split("\n")[0];
  if(/^pwd\s*&&\s*rg\s+--files/.test(first))return 'Inspect working directory and files';
  if(/^python[\d.]*\s+-\s*<</.test(first))return 'Run Python script';
  if(/^node\s+(?:--input-type=module\s+)?-\s*<</.test(first))return 'Run JavaScript';
  const read =
    /^(?:cat|head|tail|sed\s+-n\s+(?:'[^']*'|"[^"]*"|\S+))\s+(.+)$/.exec(first);
  if (read && !/[;&|]/.test(read[1]))
    return `Read ${read[1].replace(/["']/g, "").split("/").at(-1)}`;
  if (/^rg\s/.test(first)) return `Search · ${first.slice(3)}`;
  return first.replace(/^(?:\/[\w. -]+)+\/(agent-archive|python[\d.]*|node|git|rg)\b/,'$1');
}

export function operationSummary(operations) {
  if (!operations.length) return "Run script";
  const labels = operations.map((op) => op.title);
  return `${operations.length === 1 ? "" : `${operations.length} operations · `}${labels.slice(0, 2).join(" · ")}${operations.length > 2 ? " …" : ""}`;
}
