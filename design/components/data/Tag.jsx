import React from "react";

/**
 * Tag — monospace chip for adapters, providers, environments and labels.
 * Optional leading dot/icon and a remove affordance.
 */
export function Tag({ icon, dotColor, onRemove, interactive = false, className = "", children, ...rest }) {
  const cls = [
    "ds-tag",
    (interactive || rest.onClick) && "ds-tag--interactive",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <span className={cls} {...rest}>
      {dotColor && <span className="ds-tag__dot" style={{ background: dotColor }} />}
      {icon}
      {children}
      {onRemove && (
        <span
          className="ds-tag__remove"
          role="button"
          aria-label="Remove"
          onClick={(e) => {
            e.stopPropagation();
            onRemove(e);
          }}
        >
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </span>
      )}
    </span>
  );
}
