import React from "react";

/**
 * ProgressBar — single-value bar, indeterminate "running" shimmer, or a
 * segmented pass/fail/error/skip distribution.
 */
export function ProgressBar({ value = 0, segments, running = false, thin = false, className = "" }) {
  const cls = [
    "ds-progress",
    running && "ds-progress--running",
    thin && "ds-progress--thin",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  if (segments && segments.length) {
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    return (
      <div className={cls} role="progressbar">
        {segments.map((s, i) => (
          <div
            key={i}
            className={`ds-progress__seg ds-progress__seg--${s.kind}`}
            style={{ width: `${(s.value / total) * 100}%` }}
            title={`${s.kind}: ${s.value}`}
          />
        ))}
      </div>
    );
  }

  return (
    <div className={cls} role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100}>
      <div className="ds-progress__bar" style={{ width: `${running ? 100 : value}%` }} />
    </div>
  );
}
