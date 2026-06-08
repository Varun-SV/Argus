import React from "react";

/**
 * IconButton — square, icon-only control for toolbars and dense UI.
 */
export function IconButton({
  size = "md",
  variant = "ghost",
  active = false,
  label,
  className = "",
  children,
  ...rest
}) {
  const cls = [
    "ds-iconbtn",
    size === "sm" && "ds-iconbtn--sm",
    variant === "solid" && "ds-iconbtn--solid",
    active && "ds-iconbtn--active",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button className={cls} aria-label={label} title={label} {...rest}>
      {children}
    </button>
  );
}
