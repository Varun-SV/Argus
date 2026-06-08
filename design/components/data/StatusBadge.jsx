import React from "react";

const LABELS = {
  pass: "Passed",
  fail: "Failed",
  error: "Error",
  running: "Running",
  skipped: "Skipped",
  flaky: "Flaky",
};

/**
 * StatusBadge — the canonical representation of a test outcome.
 * status: pass | fail | error | running | skipped | flaky
 */
export function StatusBadge({ status = "pass", label, solid = false, className = "", ...rest }) {
  const cls = [
    "ds-status",
    `ds-status--${status}`,
    solid && "ds-status--solid",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} {...rest}>
      <span className="ds-status__dot" />
      {label || LABELS[status] || status}
    </span>
  );
}
