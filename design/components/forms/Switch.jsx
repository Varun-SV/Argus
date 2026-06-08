import React from "react";

/**
 * Switch — boolean toggle with optional label.
 */
export function Switch({ checked, defaultChecked, onChange, label, disabled = false, id, ...rest }) {
  const fieldId = id || React.useId();
  return (
    <label className="ds-switch" htmlFor={fieldId} data-disabled={disabled || undefined}>
      <input
        id={fieldId}
        type="checkbox"
        checked={checked}
        defaultChecked={defaultChecked}
        onChange={onChange}
        disabled={disabled}
        {...rest}
      />
      <span className="ds-switch__track">
        <span className="ds-switch__thumb" />
      </span>
      {label && <span className="ds-switch__label">{label}</span>}
    </label>
  );
}
