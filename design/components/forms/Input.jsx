import React from "react";

/**
 * Input — labelled text field with optional leading icon, hint and error.
 */
export function Input({
  label,
  hint,
  error,
  required = false,
  leadingIcon,
  mono = false,
  id,
  className = "",
  ...rest
}) {
  const fieldId = id || React.useId();
  const inputCls = [
    "ds-input",
    mono && "ds-input--mono",
    error && "ds-input--error",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="ds-field">
      {label && (
        <label className="ds-label" htmlFor={fieldId}>
          {label}
          {required && <span className="ds-label__req">*</span>}
        </label>
      )}
      <div className={`ds-input-wrap${leadingIcon ? " ds-input-wrap--icon" : ""}`}>
        {leadingIcon && <span className="ds-input-wrap__icon">{leadingIcon}</span>}
        <input id={fieldId} className={inputCls} aria-invalid={!!error} {...rest} />
      </div>
      {(hint || error) && (
        <span className={`ds-field__hint${error ? " ds-field__hint--error" : ""}`}>
          {error || hint}
        </span>
      )}
    </div>
  );
}
