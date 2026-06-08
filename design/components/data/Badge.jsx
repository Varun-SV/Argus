import React from "react";

/**
 * Badge — small count or neutral label (notifications, list counts).
 */
export function Badge({ variant = "neutral", className = "", children, ...rest }) {
  const cls = [
    "ds-badge",
    variant !== "neutral" && `ds-badge--${variant}`,
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} {...rest}>
      {children}
    </span>
  );
}
