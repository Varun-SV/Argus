import React from "react";

/**
 * Checkbox — labelled boolean with a custom check glyph.
 */
export function Checkbox({ checked, defaultChecked, onChange, label, disabled = false, id, ...rest }) {
  const fieldId = id || React.useId();
  return (
    <label className="ds-checkbox" htmlFor={fieldId}>
      <input
        id={fieldId}
        type="checkbox"
        checked={checked}
        defaultChecked={defaultChecked}
        onChange={onChange}
        disabled={disabled}
        {...rest}
      />
      <span className="ds-checkbox__box">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </span>
      {label && <span className="ds-checkbox__label">{label}</span>}
    </label>
  );
}
