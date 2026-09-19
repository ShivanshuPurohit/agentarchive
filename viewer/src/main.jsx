import React, {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createRoot } from "react-dom/client";
import { GroupLabel, Icon } from "../claude/primitives.js";
import { Fields, Message, Tool } from "./renderers.jsx";
import { parse } from "./model.js";
import { Activity } from "./activity.jsx";
import { Attachments } from "./media.jsx";
import { TurnAnalysis } from "./turns.jsx";
import { TokenBadge } from "./usage.jsx";
import "./viewer.css";
import "katex/dist/katex.min.css";
import "@xterm/xterm/css/xterm.css";

async function api(url, signal) {
  const r = await fetch(url, { signal, credentials: "same-origin" });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Request failed");
  return d;
}
const initial = new URLSearchParams(location.hash.slice(1));
const ranks = { summary: 0, normal: 1, thinking: 2, verbose: 3 };
const shortPath = (value) =>
  String(value || "Other sessions")
    .split("/")
    .filter(Boolean)
    .slice(-1)[0];
function IconButton({ name, label, onClick }) {
  return (
    <button
      className="archive-icon"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      <Icon name={name} size="sm" />
    </button>
  );
}

function SessionGroup({ name, sources, selected, onSelect }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <section className="group/section">
      <GroupLabel
        kind="group"
        collapsed={collapsed}
        onToggle={() => setCollapsed(!collapsed)}
      >
        {shortPath(name)}
      </GroupLabel>
      {!collapsed &&
        sources.map((source) => (
          <button
            key={source.source_id}
            data-row=""
            data-selected={
              selected === source.source_id ? "focused" : undefined
            }
            className="archive-session w-full shrink-0 border-none text-left text-secondary flex items-center gap-[var(--df-row-gap)] h-[var(--df-row-h)] px-[var(--df-row-px)] rounded-[var(--df-radius-pill)] hover:bg-[var(--df-hover)] data-[selected=focused]:bg-[var(--df-selected)] data-[selected=focused]:text-primary"
            title={source.title}
            onClick={() => onSelect(source.source_id)}
          >
            <span className="df-leading-slot">
              <Icon
                name={source.harness === "claude" ? "Chat" : "Code"}
                size="sm"
              />
            </span>
            <span data-row-label="" className="flex-1 min-w-0 truncate">
              {source.title}
            </span>
          </button>
        ))}
    </section>
  );
}
function Event({ item, source, mode }) {
  if (item.kind === "tool")
    return <Tool item={item} source={source} verbose={mode === "verbose"} />;
  if (item.kind === "reasoning")
    return (
      <details
        className="archive-thinking"
        open={mode === "thinking" || mode === "verbose"}
      >
        <summary>
          <Icon name="CaretRight" size="xs" />
          Thinking{item.opaque ? " · plaintext unavailable" : ""}
        </summary>
        <Message item={item} source={source} />
      </details>
    );
  if (item.kind === "message")
    return (
      <div
        className={item.role === "user" ? "archive-user" : "archive-assistant"}
      >
        <Message item={item} source={source} />
      </div>
    );
  if (item.kind === "attachment")
    return <Attachments part={item} source={source} />;
  if (item.kind === "boundary")
    return (
      <div className="archive-boundary">
        <Icon name="ArrowSplitRight" size="sm" />
        <Message item={item} source={source} />
      </div>
    );
  return (
    <details className="archive-metadata">
      <summary>{item.title || item.kind}</summary>
      <Fields value={parse(item.text_preview) || item.text_preview} />
    </details>
  );
}

function App() {
  const [analysis, setAnalysis] = useState(false);
  const [source, setSource] = useState(initial.get("source") || ""),
    [page, setPage] = useState(null),
    [items, setItems] = useState([]);
  const [mode, setMode] = useState("normal"),
    [query, setQuery] = useState(""),
    [harness, setHarness] = useState(""),
    [children, setChildren] = useState(false);
  const [sessions, setSessions] = useState([]),
    [cursor, setCursor] = useState(null),
    [total, setTotal] = useState(0),
    [evidence, setEvidence] = useState(false),
    [hits, setHits] = useState([]);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [sidebar, setSidebar] = useState(true),
    [showActivity, setShowActivity] = useState(false),
    [glass, setGlass] = useState(false),
    [theme, setTheme] = useState("dark");
  const scroll = useRef(null),
    search = useRef(null),
    request = useRef(null),
    anchor = useRef(null);
  const groups = useMemo(() => {
    const result = new Map();
    for (const s of sessions) {
      const key = s.project || s.harness;
      result.set(key, [...(result.get(key) || []), s]);
    }
    return [...result];
  }, [sessions]);
  useEffect(() => {
    document.documentElement.dataset.mode = theme;
  }, [theme]);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      const path = evidence
        ? `/api/search?q=${encodeURIComponent(query)}&limit=30`
        : `/api/sessions?${new URLSearchParams({ q: query, harness, children: children ? "1" : "0", limit: "100" })}`;
      if (evidence && !query) {
        setHits([]);
        return;
      }
      api(path, controller.signal)
        .then((data) => {
          if (evidence) setHits(data.hits);
          else {
            setSessions(data.sessions);
            setCursor(data.next_cursor);
            setTotal(data.total);
          }
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        });
    }, 180);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, harness, children, evidence]);
  async function moreSessions() {
    try {
      const data = await api(
        `/api/sessions?${new URLSearchParams({ q: query, harness, children: children ? "1" : "0", cursor, limit: "100" })}`,
      );
      setSessions((old) => [...old, ...data.sessions]);
      setCursor(data.next_cursor);
    } catch (e) {
      setError(e.message);
    }
  }
  function rememberAnchor() {
    const box = scroll.current;
    if (!box) return;
    const top = box.getBoundingClientRect().top;
    const first = [...box.querySelectorAll("[data-event-line]")].find(
      (el) => el.getBoundingClientRect().bottom > top,
    );
    if (first)
      anchor.current = {
        line: first.dataset.eventLine,
        offset: first.getBoundingClientRect().top - top,
      };
  }
  function focus(line) {
    requestAnimationFrame(() =>
      (
        scroll.current?.querySelector(`[data-event-line="${line}"]`) ||
        [...(scroll.current?.querySelectorAll("[data-event-line]") || [])]
          .reverse()
          .find((el) => Number(el.dataset.eventLine) <= line)
      )?.scrollIntoView({ block: "center", behavior: "smooth" }),
    );
  }
  async function load(id, from = 1, where = "replace", focusLine = 0) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    if (where !== "replace") rememberAnchor();
    try {
      const data = await api(
        `/api/transcripts/${id}?from_line=${from}&limit=120&focus_line=${focusLine}`,
        controller.signal,
      );
      setPage((old) =>
        where === "replace"
          ? data
          : {
              ...data,
              previous_line:
                where === "earlier" ? data.previous_line : old.previous_line,
              next_line: where === "later" ? data.next_line : old.next_line,
            },
      );
      setItems((old) =>
        where === "replace"
          ? data.items
          : [
              ...new Map(
                [...old, ...data.items].map((item) => [
                  `${item.line}:${item.index}`,
                  item,
                ]),
              ).values(),
            ].sort((a, b) => a.line - b.line || a.index - b.index),
      );
      history.replaceState(
        null,
        "",
        `#source=${id}&line=${focusLine || data.from_line}`,
      );
      if (where === "replace") {
        anchor.current = null;
        scroll.current?.scrollTo({ top: 0 });
        if (focusLine) focus(data.focus_line || focusLine);
      }
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      if (request.current === controller) setBusy(false);
    }
  }
  useEffect(() => {
    if (source)
      load(
        source,
        Number(initial.get("from") || 1),
        "replace",
        source === initial.get("source") ? Number(initial.get("line") || 0) : 0,
      );
    return () => request.current?.abort();
  }, [source]);
  useLayoutEffect(() => {
    if (anchor.current) {
      const saved = anchor.current;
      const el = scroll.current?.querySelector(
        `[data-event-line="${saved.line}"]`,
      );
      if (el)
        scroll.current.scrollTop +=
          el.getBoundingClientRect().top -
          scroll.current.getBoundingClientRect().top -
          saved.offset;
      anchor.current = null;
    }
  }, [items]);
  useEffect(() => {
    const handle = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        search.current?.focus();
      }
      if (e.key === "Escape") setSidebar(false);
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, []);
  function choose(id, line = 0) {
    if (id === source) load(id, 1, "replace", line);
    else {
      initial.set("source", id);
      initial.set("line", String(line));
      setSource(id);
    }
    if (innerWidth < 750) setSidebar(false);
  }
  const visible = items.filter(
    (item) => ranks[item.visibility || "verbose"] <= ranks[mode],
  );
  return (
    <div
      className={`archive-shell dframe-root ${glass ? "archive-glass" : ""}`}
      data-variant="desktop"
      data-frame-mode="code"
      data-sidebar={sidebar}
    >
      <aside
        className="archive-sidebar dframe-sidebar dframe-nav-scroll"
        data-density="comfortable"
        data-variant="desktop"
        aria-label="Archive navigation"
      >
        <div className="archive-brand">
          <span>AgentArchive</span>
          <IconButton
            name="Sidebar"
            label="Close sidebar"
            onClick={() => setSidebar(false)}
          />
        </div>
        <div className="archive-switch">
          <button aria-pressed={!evidence} onClick={() => setEvidence(false)}>
            Sessions
          </button>
          <button aria-pressed={evidence} onClick={() => setEvidence(true)}>
            Evidence
          </button>
        </div>
        <label className="archive-search">
          <Icon name="Search" size="sm" />
          <input
            ref={search}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={evidence ? "Search all evidence" : "Search sessions"}
            aria-label="Search archive"
          />
          <kbd>⌘K</kbd>
        </label>
        <div className="archive-filters">
          <select
            aria-label="Harness"
            value={harness}
            onChange={(e) => setHarness(e.target.value)}
          >
            <option value="">Claude + Codex</option>
            <option value="claude">Claude</option>
            <option value="codex">Codex</option>
          </select>
          <label>
            <input
              type="checkbox"
              checked={children}
              onChange={(e) => setChildren(e.target.checked)}
            />
            Subagents
          </label>
        </div>
        <div className="archive-session-list">
          {evidence
            ? hits.map((hit, i) => (
                <button
                  className="archive-hit"
                  key={i}
                  disabled={!hit.source_id}
                  onClick={() => choose(hit.source_id, hit.line_no || 1)}
                >
                  <span>{hit.harness}</span>
                  <p>{hit.text || hit.snippet || hit.title}</p>
                </button>
              ))
            : groups.map(([name, list]) => (
                <SessionGroup
                  key={name}
                  name={name}
                  sources={list}
                  selected={source}
                  onSelect={choose}
                />
              ))}
          {!evidence && cursor && (
            <button className="archive-more" onClick={moreSessions}>
              Load more sessions
            </button>
          )}
        </div>
        <footer className="archive-sidebar-footer">
          {sessions.length} of {total} sessions · local archive
        </footer>
      </aside>
      <section className="archive-workspace epitaxy-root">
        <header className="archive-topbar df-titlebar">
          <div className="archive-heading">
            <IconButton
              name="Sidebar"
              label="Toggle sidebar"
              onClick={() => setSidebar(!sidebar)}
            />
            <div>
              <h1>
                {page?.title ||
                  page?.source?.title ||
                  "Your work, fully rendered"}
              </h1>
              <p>
                {page
                  ? [
                      page.source.harness,
                      page.model || page.source.model,
                      shortPath(page.cwd || page.source.project),
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : "Claude and Codex rollouts"}
              </p>
            </div>
          </div>
          <div className="archive-actions">
            <button
              className="archive-control"
              disabled={!source}
              aria-pressed={analysis}
              onClick={() => setAnalysis(!analysis)}
            >
              Turn analysis
            </button>
            <button
              className="archive-control"
              aria-pressed={showActivity}
              onClick={() => setShowActivity(!showActivity)}
            >
              <Icon name="Activity" size="sm" />
              Activity
            </button>
            <select
              aria-label="Transcript view"
              value={mode}
              onChange={(e) => setMode(e.target.value)}
            >
              <option value="normal">Normal</option>
              <option value="thinking">Thinking</option>
              <option value="verbose">Verbose</option>
              <option value="summary">Messages</option>
            </select>
            <IconButton
              name={theme === "dark" ? "Sun" : "Moon"}
              label="Toggle light and dark"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            />
            <button
              className="archive-control"
              aria-pressed={glass}
              onClick={() => setGlass(!glass)}
              title="Optional glass navigation"
            >
              Glass
            </button>
          </div>
        </header>
        {error && (
          <div role="alert" className="archive-error">
            {error}
          </div>
        )}
        {showActivity && <Activity items={items} onFocus={focus} />}
        {page?.related?.length > 0 && (
          <nav className="archive-family" aria-label="Related rollouts">
            <label>
              Related rollouts{" "}
              <select
                aria-label="Open related rollout"
                value=""
                onChange={(e) => choose(e.target.value)}
              >
                <option value="" disabled>
                  Parent and subagents ({page.related.length})
                </option>
                {page.related.map((s) => (
                  <option key={s.source_id} value={s.source_id}>
                    {s.kind === "main" ? "Parent" : "Agent"} · {s.title}
                  </option>
                ))}
              </select>
            </label>
          </nav>
        )}
        <main className="archive-scroll" ref={scroll} aria-busy={busy}>
          {analysis && source ? (
            <TurnAnalysis
              source={source}
              line={page?.from_line}
              onJump={(line) => {
                setAnalysis(false);
                load(source, 1, "replace", line);
              }}
            />
          ) : !page ? (
            <div className="archive-empty">
              <Icon name="Chat" size="xxl" />
              <h2>Open a rollout</h2>
              <p>
                Read the conversation, inspect the work, and follow the recorded
                activity.
              </p>
            </div>
          ) : (
            <article className="archive-transcript epitaxy-transcript-typography">
              <nav className="archive-pagination">
                {page.previous_line ? (
                  <button
                    disabled={busy}
                    onClick={() => load(source, page.previous_line, "earlier")}
                  >
                    ↑ Earlier events
                  </button>
                ) : (
                  <span>Beginning of retained source</span>
                )}
                <button
                  disabled={busy}
                  onClick={() => load(source, 1, "replace")}
                >
                  Refresh
                </button>
              </nav>
              {visible.map((item) => (
                <section
                  className="archive-event"
                  data-event-line={item.line}
                  key={`${source}:${item.line}:${item.index}`}
                >
                  <Event item={item} source={source} mode={mode} />
                  <TokenBadge usage={item.usage} />
                  {mode === "verbose" && (
                    <div className="archive-source-ref">
                      Line {item.line}
                      {item.timestamp
                        ? ` · ${new Date(item.timestamp).toLocaleString()}`
                        : ""}
                    </div>
                  )}
                </section>
              ))}
              <nav className="archive-pagination">
                {page.next_line ? (
                  <button
                    disabled={busy}
                    onClick={() => load(source, page.next_line, "later")}
                  >
                    {busy ? "Loading…" : "↓ Later events"}
                  </button>
                ) : (
                  <span>End of retained source</span>
                )}
                <span>
                  {page.freshness?.live_delta
                    ? "Live source is newer than search index"
                    : "Recorded transcript"}
                </span>
              </nav>
            </article>
          )}
        </main>
      </section>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
