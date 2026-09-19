import React, { useEffect, useState } from "react";
import { Icon } from "../claude/primitives.js";
import "./turns.css";

const short = (n) =>
  n == null
    ? "—"
    : n >= 1000000
      ? `${(n / 1000000).toFixed(2)}m`
      : n >= 1000
        ? `${(n / 1000).toFixed(1)}k`
        : String(n);
const exact = (n) => (n == null ? "Not recorded" : n.toLocaleString());

function TokenBar({ total, part, maximum, label, partLabel, kind }) {
  return (
    <div
      className={`archive-token-bar ${kind}`}
      title={`${label}: ${exact(total)} · ${partLabel}: ${exact(part)}`}
    >
      {total != null && (
        <div
          style={{
            width: `${Math.max(0.8, (total / Math.max(1, maximum)) * 100)}%`,
          }}
        >
          {part != null && total > 0 && (
            <span
              style={{ width: `${Math.min(100, (part / total) * 100)}%` }}
            />
          )}
        </div>
      )}
    </div>
  );
}
function Metric({ turn, name, label }) {
  const known = turn.known[name],
    total = turn.steps;
  return (
    <div className="archive-metric">
      <span>{label}</span>
      <strong>{known ? short(turn.totals[name]) : "—"}</strong>
      <small>
        {known === total && total > 0
          ? "Recorded tokens"
          : `${known}/${total} usage records have counts`}
      </small>
    </div>
  );
}
export function TurnAnalysis({ source, line, onJump }) {
  const [data, setData] = useState(null),
    [selected, setSelected] = useState(line || 1),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    setSelected(line || 1);
    setData(null);
  }, [source]);
  async function load(turn, after = 0, signal) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/transcripts/${source}/usage?line=${turn}&after=${after}`,
        { signal },
      );
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Usage is unavailable");
      setData((old) =>
        after ? { ...result, steps: [...old.steps, ...result.steps] } : result,
      );
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    const controller = new AbortController();
    if (source) load(selected, 0, controller.signal);
    return () => controller.abort();
  }, [source, selected]);
  const turn = data?.selected,
    steps = data?.steps || [];
  const maxInput = Math.max(1, ...steps.map((s) => s.input || 0)),
    maxOutput = Math.max(1, ...steps.map((s) => s.output || 0));
  return (
    <section
      className="archive-turn-analysis"
      aria-label="Turn analysis"
      aria-busy={busy}
    >
      <div className="archive-analysis-heading">
        <div>
          <h2>Inside the turn</h2>
          <p>Recorded model usage between user prompts</p>
        </div>
        <button className="archive-control" onClick={() => load(selected)}>
          <Icon name="ArrowClockwise" size="sm" />
          Refresh
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {data && (
        <label className="archive-turn-picker">
          User prompt
          <select
            aria-label="Analyze user turn"
            value={turn?.line || ""}
            onChange={(e) => setSelected(Number(e.target.value))}
          >
            {data.turns.map((t) => (
              <option key={t.line} value={t.line}>
                {t.title || "User prompt"}
              </option>
            ))}
          </select>
        </label>
      )}
      {turn ? (
        <>
          <p className="archive-turn-prompt">{turn.title}</p>
          <div className="archive-turn-counts">
            <span>{turn.steps} model usage records</span>
            <span>{turn.tool_calls} tool calls</span>
            <button onClick={() => onJump(turn.line)}>
              Read this turn <Icon name="ArrowRight" size="xs" />
            </button>
          </div>
          {(turn.inherited_steps > 0 || turn.unattributed_steps > 0) && (
            <p className="archive-usage-caveat">
              Totals exclude {turn.inherited_steps} inherited parent receipts
              {turn.unattributed_steps > 0 && ` and ${turn.unattributed_steps} receipts with unknown ownership`}.
              Their recorded counts remain inspectable below.
            </p>
          )}
          <div className="archive-metrics">
            <Metric turn={turn} name="input" label="Input across calls" />
            <Metric turn={turn} name="uncached" label="Uncached input" />
            <Metric turn={turn} name="output" label="Generated output" />
            <Metric turn={turn} name="reasoning" label="Thinking / reasoning" />
          </div>
          <div className="archive-usage-legend">
            <span>
              <i className="uncached" />
              Uncached input
            </span>
            <span>
              <i className="cached" />
              Cache reads
            </span>
            <span>
              <i className="output" />
              Other output
            </span>
            <span>
              <i className="reasoning" />
              Thinking / reasoning
            </span>
          </div>
          <div className="archive-calls-table">
            <table>
              <thead>
                <tr>
                  <th>Model call / receipt</th>
                  <th>
                    Input <small>scale {short(maxInput)}</small>
                  </th>
                  <th>Uncached</th>
                  <th>Cached</th>
                  <th>
                    Output <small>scale {short(maxOutput)}</small>
                  </th>
                  <th>Thinking</th>
                </tr>
              </thead>
              <tbody>
                {steps.map((step, i) => (
                  <tr key={step.identity}>
                    <td>
                      <button
                        onClick={() => onJump(step.line)}
                        title={`${step.model || "Model unavailable"} · ${step.basis} · line ${step.usage_line || step.line}`}
                      >
                        <span className="archive-call-number">{i + 1}</span>
                        <span>
                          {step.timestamp
                            ? new Date(step.timestamp).toLocaleTimeString()
                            : "No timestamp"}
                          <small>{step.model || "Unknown model"}</small>
                        </span>
                      </button>
                      {step.basis !== "message usage" &&
                        step.basis !== "last response usage" &&
                        step.basis !== "response usage" && (
                          <small className="archive-usage-caveat">
                            {step.basis}
                          </small>
                        )}
                      {step.server_iterations > 1 && (
                        <small>
                          {step.server_iterations} server iterations
                        </small>
                      )}
                      {step.ownership !== "native" && (
                        <small className="archive-usage-caveat">
                          {step.ownership === "inherited" ? "Inherited parent usage" : "Agent ownership unknown"} · excluded from totals
                        </small>
                      )}
                    </td>
                    <td title={exact(step.input)}>
                      {short(step.input)}
                      <TokenBar
                        total={step.input}
                        part={step.cached}
                        maximum={maxInput}
                        label="Total input"
                        partLabel="Cache reads"
                        kind="input"
                      />
                    </td>
                    <td
                      title={`Uncached input: ${exact(step.uncached)} · cache writes: ${exact(step.cache_write)}`}
                    >
                      {short(step.uncached)}
                    </td>
                    <td title={exact(step.cached)}>{short(step.cached)}</td>
                    <td title={exact(step.output)}>
                      {short(step.output)}
                      <TokenBar
                        total={step.output}
                        part={step.reasoning}
                        maximum={maxOutput}
                        label="Total output"
                        partLabel="Thinking / reasoning"
                        kind="output"
                      />
                    </td>
                    <td title={exact(step.reasoning)}>
                      {short(step.reasoning)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!steps.length && (
            <p className="text-muted">
              No per-call usage was retained for this turn. The conversation and
              tool records are still available.
            </p>
          )}
          {data.next_after && (
            <button
              className="archive-more"
              disabled={busy}
              onClick={() => load(selected, data.next_after)}
            >
              Load next 200 calls
            </button>
          )}
          <p className="archive-usage-note">
            Thinking is included in total output. Input totals count every
            recorded model call, including reused context. Cache writes are
            included in uncached input. A dash means the count was not recorded.
            Child agents have their own rollouts and are not added to these
            totals. Identifiable copied parent history is excluded; missing
            receipts and provider-side operations may leave gaps.
          </p>
          <p className="archive-usage-note">
            These are token counts and receipt times. Prefill duration,
            continuous decode time, token emission times, batching, and GPU
            utilization are not recorded here. Tool-free gaps remain
            unclassified.
          </p>
          {data.parse_errors > 0 && (
            <p className="archive-usage-caveat">
              {data.parse_errors} source records could not be read; coverage is
              incomplete.
            </p>
          )}
        </>
      ) : (
        <p className="text-muted">
          {busy ? "Reading usage…" : "Select a rollout to inspect its turns."}
        </p>
      )}
    </section>
  );
}
