import React, { useMemo, useState } from "react";
import { activity, elapsed } from "./model.js";

export function Activity({ items, onFocus }) {
  const events = useMemo(() => activity(items), [items]);
  const [selected, setSelected] = useState(null);
  if (!events.length)
    return (
      <div className="archive-activity text-caption text-muted">
        No timestamps are recorded for this loaded portion.
      </div>
    );
  const start = Math.min(...events.map((e) => e.from)),
    end = Math.max(...events.map((e) => e.to)),
    width = Math.max(1, end - start);
  const ends = [];
  const arranged = events.map((event) => {
    if (event.lane !== "Tools") return { ...event, row: 0 };
    let row = ends.findIndex((end) => end <= event.from);
    if (row < 0) row = ends.length;
    ends[row] = event.to;
    return { ...event, row };
  });
  const lanes = ["User", "Model", "Tools"];
  return (
    <section className="archive-activity" aria-label="Recorded activity">
      <div className="archive-activity-title">
        <span>Recorded activity · loaded portion</span>
        <span>{elapsed(width)}</span>
      </div>
      <div className="archive-time-axis">
        <span>0</span>
        <span>{elapsed(width / 2)}</span>
        <span>{elapsed(width)}</span>
      </div>
      {lanes.map((lane) => (
        <div className="archive-lane" key={lane}>
          <span>{lane}</span>
          <div
            className="archive-track"
            style={{
              height: Math.max(1, lane === "Tools" ? ends.length : 1) * 18 + 4,
            }}
          >
            {arranged
              .filter((e) => e.lane === lane)
              .map((event, i) => (
                <button
                  key={`${event.line}:${i}`}
                  className={
                    event.point ? "archive-time-dot" : "archive-time-span"
                  }
                  style={{
                    top: event.row * 18 + 6,
                    left: `${((event.from - start) / width) * 96}%`,
                    width: event.point
                      ? undefined
                      : `${Math.max(0.3, ((event.to - event.from) / width) * 96)}%`,
                  }}
                  title={`${event.label} · ${event.basis || "recorded event"}${!event.point ? " · " + elapsed(event.to - event.from) : ""}`}
                  aria-label={`${event.label} at ${elapsed(event.from - start)}`}
                  onClick={() => {
                    setSelected(event);
                    onFocus(event.line);
                  }}
                />
              ))}
          </div>
        </div>
      ))}
      <div className="text-caption text-muted archive-activity-note">
        {selected
          ? `${selected.label} · ${new Date(selected.from).toLocaleTimeString()} · ${selected.basis || "message timestamp"}`
          : "Dots are recorded messages. Bars are tool intervals; gaps do not establish inference idle time."}
      </div>
    </section>
  );
}
