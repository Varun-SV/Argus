import React from "react";

/**
 * Button — primary action control for Argus surfaces.
 * Variants: primary | secondary | ghost | danger. Sizes: sm | md | lg.
 */
export function Button({
  variant = "primary",
  size = "md",
  leftIcon,
  rightIcon,
  loading = false,
  block = false,
  disabled = false,
  className = "",
  children,
  ...rest
}) {
  const cls = [
    "ds-btn",
    `ds-btn--${variant}`,
    size !== "md" && `ds-btn--${size}`,
    block && "ds-btn--block",
    loading && "ds-btn--loading",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button className={cls} disabled={disabled || loading} {...rest}>
      {leftIcon && <span className="ds-btn__icon">{leftIcon}</span>}
      {children}
      {rightIcon && <span className="ds-btn__icon">{rightIcon}</span>}
    </button>
  );
}
