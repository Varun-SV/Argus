import React from "react";

/**
 * Select — styled native select with a custom chevron. Pass <option>s as children
 * or an `options` array of { value, label }.
 */
export function Select({ label, hint, options, id, className = "", children, ...rest }) {
  const fieldId = id || React.useId();
  const select = (
    <div className="ds-select-wrap">
      <select id={fieldId} className={`ds-select ${className}`} {...rest}>
        {options
          ? options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))
          : children}
      </select>
    </div>
  );

  if (!label && !hint) return select;
  return (
    <div className="ds-field">
      {label && (
        <label className="ds-label" htmlFor={fieldId}>
          {label}
        </label>
      )}
      {select}
      {hint && <span className="ds-field__hint">{hint}</span>}
    </div>
  );
}
