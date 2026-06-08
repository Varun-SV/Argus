import React from "react";

/**
 * Tooltip — hover/focus bubble. Wraps a single trigger element.
 */
export function Tooltip({ content, kbd, children, className = "", ...rest }) {
  return (
    <span className={`ds-tooltip ${className}`} {...rest}>
      {children}
      <span className="ds-tooltip__bubble" role="tooltip">
        {content}
        {kbd && <span className="ds-tooltip__kbd">{kbd}</span>}
      </span>
    </span>
  );
}
