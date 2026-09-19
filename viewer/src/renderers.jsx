import React, { useEffect, useMemo, useState } from "react";
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";
import { createTwoFilesPatch } from "diff";
import { Icon, FieldList } from "./ui.jsx";
import {
  displayValue,
  elapsed,
  parse,
  textBlocks,
  toolModel,
  resultReceipts,
} from "./model.js";
import { Attachments, Csv } from "./media.jsx";
import { Code, Terminal } from "./code.jsx";
import { TokenBadge } from "./usage.jsx";
export { Code } from "./code.jsx";

const clean = (html) =>
  DOMPurify.sanitize(html, {
    ADD_ATTR: ["target"],
    FORBID_TAGS: ["style", "iframe"],
    FORBID_ATTR: ["style"],
  });
const md = new MarkdownIt({ html: false, linkify: true, breaks: false });
md.renderer.rules.image = () => ""; // Attachments use source-scoped routes below.
const safeLink = (url) => /^(https?:|mailto:|#)/i.test(url);
const defaultLink =
  md.renderer.rules.link_open ||
  ((tokens, idx, options, env, self) => self.renderToken(tokens, idx, options));
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  const token = tokens[idx],
    href = token.attrGet("href") || "";
  if (!safeLink(href)) token.attrSet("href", "#");
  token.attrSet("target", "_blank");
  token.attrSet("rel", "noreferrer noopener");
  return defaultLink(tokens, idx, options, env, self);
};
function Diagram({ text }) {
  const [svg, setSvg] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    import("mermaid")
      .then(async ({ default: mermaid }) => {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          htmlLabels: false,
          flowchart: { htmlLabels: false },
          theme:
            document.documentElement.dataset.mode === "dark"
              ? "dark"
              : "default",
        });
        const { svg } = await mermaid.render(
          `diagram-${crypto.randomUUID()}`,
          text,
        );
        if (active)
          setSvg(
            DOMPurify.sanitize(svg, {
              ADD_TAGS: ["foreignObject"],
              FORBID_TAGS: ["script", "iframe", "object", "embed", "form"],
            }),
          );
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [text]);
  return svg ? (
    <div
      className="archive-diagram"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  ) : error ? (
    <>
      <div className="text-caption text-muted">
        Diagram could not be rendered
      </div>
      <Code text={text} language="mermaid" />
    </>
  ) : (
    <span className="text-muted">Rendering diagram…</span>
  );
}
function Formula({ text }) {
  const [html, setHtml] = useState("");
  useEffect(() => {
    import("katex").then(({ default: katex }) =>
      setHtml(
        katex.renderToString(text, {
          displayMode: true,
          throwOnError: false,
          trust: false,
        }),
      ),
    );
  }, [text]);
  return html ? (
    <div className="archive-math" dangerouslySetInnerHTML={{ __html: html }} />
  ) : (
    <code>{text}</code>
  );
}
export function HtmlPreview({ html, title = "HTML preview" }) {
  return (
    <iframe
      title={title}
      sandbox=""
      referrerPolicy="no-referrer"
      className="archive-document"
      srcDoc={`<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; base-uri 'none'"><style>body{font:14px system-ui;padding:12px;overflow-wrap:anywhere}img,svg{max-width:100%}</style>${DOMPurify.sanitize(html, { ADD_TAGS: ["style"], FORBID_TAGS: ["script", "iframe", "object", "embed", "form", "base"], FORBID_ATTR: ["action", "formaction"] })}`}
    />
  );
}
function Disclosure({ title, children }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="archive-source"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>{title}</summary>
      {open && children}
    </details>
  );
}
export function Markdown({ text = "" }) {
  const tokens = useMemo(() => md.parse(String(text), {}), [text]);
  const structured = parse(text);
  if (structured && typeof structured === "object")
    return (
      <Disclosure title="JSON">
        <Code text={String(text)} language="json" />
      </Disclosure>
    );
  let cursor = 0;
  function children() {
    const nodes = [];
    while (cursor < tokens.length) {
      const token = tokens[cursor++],
        key = cursor;
      if (token.nesting === -1) break;
      if (token.type === "fence" || token.type === "code_block") {
        const lang = token.info.trim().split(/\s+/)[0].toLowerCase();
        nodes.push(
          <React.Fragment key={key}>
            {lang === "mermaid" ? (
              <Diagram text={token.content} />
            ) : ["json", "jsonc"].includes(lang) ? (
              <Disclosure
                title={`JSON · ${token.content.trimEnd().split("\n").length} lines`}
              >
                <Code text={token.content} language="json" />
              </Disclosure>
            ) : lang === "csv" ? (
              <Csv text={token.content} />
            ) : ["math", "latex", "tex"].includes(lang) ? (
              <Formula text={token.content} />
            ) : (
              <Code
                text={token.content.replace(/\n$/, "")}
                language={lang || "text"}
              />
            )}
            {["html", "svg"].includes(lang) && (
              <details className="archive-preview">
                <summary>Preview {lang.toUpperCase()}</summary>
                <HtmlPreview html={token.content} />
              </details>
            )}
          </React.Fragment>,
        );
      } else if (token.nesting === 1) {
        const content = children();
        nodes.push(
          token.hidden ? (
            <React.Fragment key={key}>{content}</React.Fragment>
          ) : (
            React.createElement(
              token.tag,
              {
                key,
                ...Object.fromEntries(
                  (token.attrs || []).filter(([name]) =>
                    ["start", "align"].includes(name),
                  ),
                ),
              },
              content,
            )
          ),
        );
      } else if (token.type === "inline")
        nodes.push(
          <span
            key={key}
            className="archive-inline"
            dangerouslySetInnerHTML={{
              __html: clean(
                md.renderer.renderInline(token.children, md.options, {}),
              ),
            }}
          />,
        );
      else if (token.type === "hr") nodes.push(<hr key={key} />);
    }
    return nodes;
  }
  return <div className="archive-prose archive-markdown">{children()}</div>;
}
export function Fields({ value }) {
  if (value == null) return null;
  if (typeof value !== "object") return <Markdown text={String(value)} />;
  if (Array.isArray(value))
    return (
      <div className="archive-list">
        {value.slice(0, 100).map((v, i) => (
          <Fields key={i} value={v} />
        ))}
        {value.length > 100 && (
          <details>
            <summary>{value.length - 100} more items</summary>
            <Code text={displayValue(value.slice(100))} language="json" />
          </details>
        )}
      </div>
    );
  const rows = Object.entries(value).filter(
    ([k]) => !["signature", "encrypted_content"].includes(k),
  );
  const simple = rows
    .filter(([, v]) => v == null || typeof v !== "object")
    .map(([label, v]) => ({ label, value: String(v ?? ""), code: false }));
  return (
    <div className="archive-fields">
      <FieldList
        rows={simple.filter(
          (row) => row.value.length < 300 && !row.value.includes("\n"),
        )}
        label="Tool details"
      />
      {simple
        .filter((row) => row.value.length >= 300 || row.value.includes("\n"))
        .map((row) => (
          <details key={row.label}>
            <summary>{row.label}</summary>
            <Markdown text={row.value} />
          </details>
        ))}
      {rows
        .filter(([, v]) => v && typeof v === "object")
        .map(([key, v]) => (
          <details key={key}>
            <summary>{key}</summary>
            <Fields value={v} />
          </details>
        ))}
    </div>
  );
}
export function Diff({ input, text }) {
  const patch =
    typeof input === "object" && typeof input?.old_string === "string"
      ? createTwoFilesPatch(
          input.file_path || "file",
          input.file_path || "file",
          input.old_string,
          input.new_string || "",
        )
      : typeof input === "string"
        ? input
        : text || displayValue(input);
  return (
    <div className="archive-diff rounded-lg border border-alpha-2 overflow-hidden">
      <div className="archive-code-title text-caption text-muted">
        {input?.file_path || "Changes"}
      </div>
      <pre>
        {String(patch)
          .split("\n")
          .map((line, i) => (
            <div
              key={i}
              className={
                line.startsWith("+")
                  ? "added"
                  : line.startsWith("-")
                    ? "removed"
                    : line.startsWith("@@")
                      ? "hunk"
                      : ""
              }
            >
              {line || " "}
            </div>
          ))}
      </pre>
    </div>
  );
}
export function Result({ text, terminal = false }) {
  return (
    <>
      {textBlocks(text).map((block, i) =>
        block.type === "fields" ? (
          <Fields key={i} value={block.value} />
        ) : terminal || /stdout|stderr/.test(block.type) ? (
          <Terminal
            key={i}
            text={block.text}
            label={block.type === "text" ? null : block.type}
          />
        ) : (
          <Markdown key={i} text={block.text} />
        ),
      )}
    </>
  );
}
function OutputDisclosure({ receipt, operation, number, terminal }) {
  const [open, setOpen] = useState(false);
  const text = typeof receipt.output === "string" ? receipt.output : "";
  const markdown =
    /\.md(?:["'\s]|$)/i.test(operation?.command || "") ||
    /^\s*(?:#{1,6} |```|\|[^\n]+\|\n|[-*] \S)/.test(text);
  const title = operation?.title || `Result ${number + 1}`;
  return (
    <details
      className="archive-result"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>
        <Icon name="CaretRight" size="xs" />
        <span className="archive-result-name">{title}</span>
        <span
          className={
            receipt.status === "error" ? "archive-failed" : "text-muted"
          }
        >
          {receipt.exit != null
            ? `Exit ${receipt.exit}`
            : receipt.status === "yielded"
              ? "Yielded"
              : receipt.status === "error"
                ? "Failed"
                : "Returned"}
        </span>
        {text && (
          <span className="text-muted">
            {text.split("\n").length.toLocaleString()} lines
          </span>
        )}
        {receipt.seconds != null && receipt.seconds >= 0.001 && (
          <span className="text-muted">{elapsed(receipt.seconds * 1000)}</span>
        )}
      </summary>
      {open && (
        <div className="archive-result-body">
          {operation?.cwd && (
            <div className="text-caption text-muted">{operation.cwd}</div>
          )}
          {operation?.command && (
            <Code text={operation.command} language="bash" />
          )}
          {markdown ? (
            <Markdown text={text} />
          ) : (
            <Result text={receipt.output} terminal={terminal} />
          )}
          <TokenBadge toolResult />
        </div>
      )}
    </details>
  );
}
function ToolOutput({ part, tool, source }) {
  const receipts = useMemo(
    () => resultReceipts(part.text_preview || ""),
    [part.text_preview],
  );
  return (
    <>
      {receipts.map((receipt, i) => (
        <OutputDisclosure
          key={i}
          receipt={receipt}
          number={i}
          operation={
            receipt.index != null ? tool.operations[receipt.index] : undefined
          }
          terminal={tool.kind === "shell" || receipt.exit != null}
        />
      ))}
      <Attachments part={part} source={source} />
    </>
  );
}
async function fullPart(source, part) {
  let text = part.text_preview || "",
    offset = Array.from(text).length;
  while (part.truncated && offset < part.char_count) {
    const response = await fetch(
      `/api/transcripts/${source}/item?line=${part.line}&index=${part.index}&offset=${offset}&limit=65536`,
    );
    if (!response.ok) throw new Error("Unable to load stored body");
    const data = await response.json();
    text += data.text;
    offset = data.next_offset;
    if (!offset) break;
  }
  return { ...part, text_preview: text, truncated: false };
}
export function Tool({ item, source, verbose = false }) {
  const [open, setOpen] = useState(false),
    [parts, setParts] = useState(item.components),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const tool = toolModel(item, parts);
  useEffect(() => {
    setParts(item.components);
  }, [item]);
  async function toggle() {
    setOpen(!open);
    if (!open && parts.some((p) => p.truncated)) {
      setLoading(true);
      try {
        setParts(await Promise.all(parts.map((p) => fullPart(source, p))));
      } catch (e) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
  }
  return (
    <div
      data-ui="TurnStatusStep"
      data-state={tool.status}
      className="archive-tool flex w-full min-w-0 flex-col"
    >
      <button
        className="archive-step"
        aria-expanded={open}
        onClick={toggle}
      >
        <Icon
          name={
            tool.status === "error"
              ? "X"
              : tool.status === "yielded"
                ? "Clock"
                : tool.kind === "shell"
                  ? "Code"
                  : tool.kind === "diff"
                    ? "File"
                    : "CaretRight"
          }
          size="sm"
        />
        <span className="text-muted shrink-0">{tool.name}</span>
        <span className="text-primary archive-tool-title">
          {String(tool.title).replace(
            new RegExp(
              `^${tool.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")} · `,
            ),
            "",
          )}
        </span>
        {tool.timing && (
          <span
            className="archive-duration text-caption text-muted"
            title={tool.timing.basis}
          >
            {elapsed(tool.timing.to - tool.timing.from)}
          </span>
        )}
        <Icon name="CaretDown" size="xs" className={open ? "rotate-180" : ""} />
      </button>
      {open && (
        <div className="archive-tool-panel" data-ui-row-panel="">
          {error && <div role="alert">{error}</div>}
          {loading && (
            <div className="text-muted">Loading full stored content…</div>
          )}
          {tool.cwd && (
            <div className="text-caption text-muted">{tool.cwd}</div>
          )}
          {tool.kind === "script" ? (
            <>
              {tool.operations.length > 0 && (
                <div className="archive-operations">
                  {tool.operations.map((operation, i) => (
                    <details key={i}>
                      <summary>
                        <Icon name="Code" size="sm" />
                        {operation.title}
                      </summary>
                      <Code
                        text={operation.command || operation.source}
                        language={operation.command ? "bash" : "javascript"}
                      />
                    </details>
                  ))}
                </div>
              )}
              <Disclosure title="Orchestration source">
                <Code text={tool.code} language="javascript" />
              </Disclosure>
            </>
          ) : tool.kind === "diff" ? (
            <Diff input={tool.input} />
          ) : tool.code ? (
            <Code
              text={String(tool.code)}
              language={tool.kind === "script" ? "javascript" : "bash"}
            />
          ) : (
            <Fields value={tool.input} />
          )}
          {tool.results
            .filter((p) => p.phase === "result")
            .map((p) => (
              <div key={`${p.line}:${p.index}`}>
                <ToolOutput part={p} tool={tool} source={source} />
              </div>
            ))}
          {tool.status === "unresolved" && (
            <div className="text-caption text-muted">
              No result recorded in this source.
            </div>
          )}
          {tool.status === "yielded" && (
            <div className="text-caption text-muted">
              Returned while execution continued. Completion, if recorded,
              appears in a later wait or poll.
            </div>
          )}
          <Disclosure title="Original recorded receipt">
            {tool.results
              .filter((p) => p.phase === "result")
              .map((p) => (
                <Code
                  key={`${p.line}:${p.index}`}
                  text={p.text_preview}
                  language="text"
                />
              ))}
          </Disclosure>
          {verbose && (
            <details>
              <summary>Recorded lifecycle and structured details</summary>
              {tool.results
                .filter((p) => p.phase !== "result")
                .map((p) => (
                  <Fields
                    key={`${p.line}:${p.index}`}
                    value={parse(p.text_preview) || p.text_preview}
                  />
                ))}
            </details>
          )}
        </div>
      )}
    </div>
  );
}
export function Message({ item, source }) {
  const [part, setPart] = useState(item),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  useEffect(() => setPart(item), [item]);
  return (
    <>
      <Markdown text={part.text_preview} />
      <Attachments part={part} source={source} />
      {error && <p role="alert">{error}</p>}
      {part.truncated && (
        <button
          className="archive-more text-caption"
          disabled={loading}
          onClick={async () => {
            setLoading(true);
            try {
              setPart(await fullPart(source, part));
            } catch (e) {
              setError(e.message);
            } finally {
              setLoading(false);
            }
          }}
        >
          {loading
            ? "Loading…"
            : `Read full content · ${part.char_count.toLocaleString()} characters`}
        </button>
      )}
    </>
  );
}
