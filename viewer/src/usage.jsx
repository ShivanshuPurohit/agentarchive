import React from "react";

const short = (n) =>
  n == null
    ? "—"
    : n >= 1000000
      ? `${(n / 1000000).toFixed(2)}m`
      : n >= 1000
        ? `${(n / 1000).toFixed(1)}k`
        : String(n);
const full = (n) => (n == null ? "not recorded" : n.toLocaleString());
export function TokenBadge({ usage, toolResult = false }) {
  const title = usage
    ? `Recorded model-call totals: ${full(usage.input)} input (${full(usage.cached)} cached), ${full(usage.output)} output (${full(usage.reasoning)} reasoning, included in output. Associated by ${usage.association}; source line ${usage.line}. Other blocks in this call repeat these totals.${usage.ownership === "inherited" ? " Inherited parent usage; excluded from this agent's totals." : usage.ownership === "unknown" ? " Agent ownership is unknown; excluded from totals." : ""}`
    : toolResult
      ? "Tool output has no recorded model-token attribution."
      : "Per-block token counts were not retained. Model-call totals are shown where they can be associated.";
  return (
    <div className="archive-token-badge" title={title} aria-label={title}>
      {short(usage?.input)} in <span>·</span> {short(usage?.output)} out
    </div>
  );
}
