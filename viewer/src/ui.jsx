import React from "react";

const paths = {
  CaretRight: "m9 5 7 7-7 7",
  CaretDown: "m5 9 7 7 7-7",
  ArrowRight: "M4 12h16m-7-7 7 7-7 7",
  ArrowClockwise: "M20 8a9 9 0 1 0 1 8M20 2v6h-6",
  ArrowSplitRight:
    "M5 3v12a4 4 0 0 0 4 4h11m-5-5 5 5-5 5M5 9a4 4 0 0 1 4-4h11m-5-5 5 5-5 5",
  Search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6",
  Sidebar: "M3 3h18v18H3zM9 3v18",
  Chat: "M21 11a9 9 0 0 1-9 9H3l2-4a9 9 0 1 1 16-5ZM8 11h.1M12 11h.1M16 11h.1",
  Code: "m7 5-6 7 6 7m10-14 6 7-6 7M14 2l-4 20",
  Check: "m4 12 5 5L20 6",
  X: "m5 5 14 14M19 5 5 19",
  Copy: "M8 8h13v13H8zM16 8V3H3v13h5",
  Clock: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20m0 4v6l4 2",
  File: "M5 2h9l5 5v15H5zM14 2v6h5",
  Activity: "M3 3h18v18H3zM5 14h3l3-7 3 10 3-5h2",
  Sun: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10M12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2",
  Moon: "M20 15A9 9 0 0 1 9 4 9 9 0 1 0 20 15Z",
};

export function Icon({ name, size = "sm", className = "" }) {
  const px = { xs: 12, sm: 16, md: 20, lg: 24, xxl: 40 }[size] || size;
  return (
    <svg
      data-ui="Icon"
      aria-hidden="true"
      className={className}
      width={px}
      height={px}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={paths[name] || paths.File} />
    </svg>
  );
}

export function GroupLabel({ children, collapsed, onToggle }) {
  return (
    <button
      className="archive-group-label"
      aria-expanded={!collapsed}
      onClick={onToggle}
    >
      <span>{children}</span>
      <Icon name={collapsed ? "CaretRight" : "CaretDown"} size="xs" />
    </button>
  );
}

export function FieldList({ rows, label }) {
  return (
    <ul aria-label={label}>
      {rows.map(({ label, value }, i) => (
        <li key={`${label}-${i}`}>
          <span className="text-muted">{label}</span>
          <span>{value}</span>
        </li>
      ))}
    </ul>
  );
}
