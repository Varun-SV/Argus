import React from "react";

/**
 * Card — surface container with optional header/footer subcomponents.
 */
export function Card({ raised = false, interactive = false, className = "", children, ...rest }) {
  const cls = [
    "ds-card",
    raised && "ds-card--raised",
    interactive && "ds-card--interactive",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} {...rest}>
      {children}
    </div>
  );
}

Card.Header = function CardHeader({ title, actions, className = "", children, ...rest }) {
  return (
    <div className={`ds-card__header ${className}`} {...rest}>
      {title && <span className="ds-card__title">{title}</span>}
      {children}
      {actions && <div style={{ marginLeft: "auto", display: "flex", gap: "var(--space-2)" }}>{actions}</div>}
    </div>
  );
};

Card.Body = function CardBody({ className = "", children, ...rest }) {
  return (
    <div className={`ds-card__body ${className}`} {...rest}>
      {children}
    </div>
  );
};

Card.Footer = function CardFooter({ className = "", children, ...rest }) {
  return (
    <div className={`ds-card__footer ${className}`} {...rest}>
      {children}
    </div>
  );
};
