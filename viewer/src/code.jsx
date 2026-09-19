import React, { useEffect, useRef, useState } from "react";
import DOMPurify from "dompurify";
import { AnsiUp } from "ansi_up";
import { Icon } from "../claude/primitives.js";
import { lightTheme, darkTheme } from "../claude/themes.js";

let highlight;
async function colorCode(text, lang) {
  highlight ||= Promise.all([
    import("shiki/core"),
    import("shiki/engine/oniguruma"),
  ]).then(([core, regex]) =>
    core.createHighlighterCore({
      themes: [lightTheme, darkTheme],
      engine: regex.createOnigurumaEngine(import("shiki/wasm")),
      langs: [
        import("@shikijs/langs/javascript"),
        import("@shikijs/langs/typescript"),
        import("@shikijs/langs/python"),
        import("@shikijs/langs/bash"),
        import("@shikijs/langs/json"),
        import("@shikijs/langs/diff"),
        import("@shikijs/langs/css"),
        import("@shikijs/langs/html"),
        import("@shikijs/langs/rust"),
        import("@shikijs/langs/markdown"),
        import("@shikijs/langs/yaml"),
        import("@shikijs/langs/sql"),
        import("@shikijs/langs/cpp"),
      ],
    }),
  );
  const engine = await highlight;
  const language =
    {
      js: "javascript",
      ts: "typescript",
      sh: "bash",
      shell: "bash",
      py: "python",
      yml: "yaml",
      c: "cpp",
    }[lang] || lang;
  return engine.codeToHtml(text, {
    lang: engine.getLoadedLanguages().includes(language) ? language : "text",
    themes: { light: "claude-light", dark: "claude-dark" },
  });
}
export function Code({ text = "", language = "text", filename }) {
  const [html, setHtml] = useState(""),
    [copied, setCopied] = useState(false);
  useEffect(() => {
    let active = true;
    colorCode(String(text), language)
      .then((value) => {
        if (active) setHtml(value);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [text, language]);
  return (
    <div className="archive-code epitaxy-codeblock relative max-w-full w-full">
      <div
        className="epitaxy-diff epitaxy-fence rounded-lg overflow-clip"
        data-code-text={text}
      >
        {html ? (
          <div
            className="archive-highlight"
            dangerouslySetInnerHTML={{ __html: html }}
          />
        ) : (
          <pre>
            <code>{text}</code>
          </pre>
        )}
      </div>
      <div className="archive-code-actions">
        <span className="text-caption text-muted">{filename || language}</span>
        <button
          className="archive-icon"
          aria-label="Copy code"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(String(text));
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            } catch {
              setCopied(false);
            }
          }}
        >
          <Icon name={copied ? "Check" : "Copy"} size="sm" />
        </button>
      </div>
    </div>
  );
}

function TerminalControl({ text }) {
  const ref = useRef(null);
  useEffect(() => {
    let term;
    let active = true;
    import("@xterm/xterm").then(({ Terminal }) => {
      if (!active) return;
      term = new Terminal({
        convertEol: true,
        disableStdin: true,
        rows: Math.min(24, Math.max(5, text.split("\n").length)),
        cols: 90,
        scrollback: 20000,
        theme: { background: "#151515", foreground: "#e1e0d9" },
        fontFamily: "ui-monospace, Menlo, monospace",
        fontSize: 12,
      });
      term.open(ref.current);
      term.write(text);
    });
    return () => {
      active = false;
      term?.dispose();
    };
  }, [text]);
  return <div className="archive-terminal-control" ref={ref} />;
}
export function Terminal({ text = "", label }) {
  const control = /\x1b\[[\d;]*[ABCDEFGHJKSTf]|\r(?!\n)/.test(text);
  return (
    <div className="archive-terminal">
      {label && (
        <div className="archive-code-title text-caption text-muted">
          {label}
        </div>
      )}
      {control ? (
        <TerminalControl text={text} />
      ) : (
        <pre
          dangerouslySetInnerHTML={{
            __html: DOMPurify.sanitize(new AnsiUp().ansi_to_html(String(text)), {
              ALLOWED_TAGS: ["span", "a"],
              ALLOWED_ATTR: ["style", "class", "href", "target", "rel"],
            }),
          }}
        />
      )}
    </div>
  );
}
