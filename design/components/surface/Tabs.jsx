import React from "react";

/**
 * Tabs — horizontal navigation. Underline (default) or segmented pill style.
 * items: { id, label, icon?, count? }[]
 */
export function Tabs({ items = [], value, onChange, variant = "underline", className = "" }) {
  const wrapCls = ["ds-tabs", variant === "pills" && "ds-tabs--pills", className]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={wrapCls} role="tablist">
      {items.map((it) => {
        const active = it.id === value;
        const tabCls = [
          "ds-tab",
          variant === "pills" && "ds-tab--pill",
          active && "ds-tab--active",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <button
            key={it.id}
            className={tabCls}
            role="tab"
            aria-selected={active}
            onClick={() => onChange && onChange(it.id)}
          >
            {it.icon}
            {it.label}
            {it.count != null && <span className="ds-tab__count">{it.count}</span>}
          </button>
        );
      })}
    </div>
  );
}
