/* @ds-bundle: {"format":3,"namespace":"ArgusDesignSystem_e0a29b","components":[{"name":"Button","sourcePath":"components/buttons/Button.jsx"},{"name":"IconButton","sourcePath":"components/buttons/IconButton.jsx"},{"name":"Badge","sourcePath":"components/data/Badge.jsx"},{"name":"StatusBadge","sourcePath":"components/data/StatusBadge.jsx"},{"name":"Tag","sourcePath":"components/data/Tag.jsx"},{"name":"ProgressBar","sourcePath":"components/feedback/ProgressBar.jsx"},{"name":"Tooltip","sourcePath":"components/feedback/Tooltip.jsx"},{"name":"Checkbox","sourcePath":"components/forms/Checkbox.jsx"},{"name":"Input","sourcePath":"components/forms/Input.jsx"},{"name":"Select","sourcePath":"components/forms/Select.jsx"},{"name":"Switch","sourcePath":"components/forms/Switch.jsx"},{"name":"Card","sourcePath":"components/surface/Card.jsx"},{"name":"Tabs","sourcePath":"components/surface/Tabs.jsx"}],"sourceHashes":{"components/buttons/Button.jsx":"59925ac404df","components/buttons/IconButton.jsx":"e6fdcd0c350e","components/data/Badge.jsx":"ba97b2f1fb08","components/data/StatusBadge.jsx":"051b2023c2cd","components/data/Tag.jsx":"70773649895f","components/feedback/ProgressBar.jsx":"bee64dee9cee","components/feedback/Tooltip.jsx":"4f6a3d738103","components/forms/Checkbox.jsx":"0a6a60ee595c","components/forms/Input.jsx":"9aab953a4da2","components/forms/Select.jsx":"c8974db32c41","components/forms/Switch.jsx":"fcc39a66b2ef","components/surface/Card.jsx":"ce3842a9c557","components/surface/Tabs.jsx":"9a277288f6d5","ui_kits/cli/app.jsx":"3ccae25fac1b","ui_kits/cli/data.js":"402c1b21b0a9","ui_kits/console/Editor.jsx":"0b032a2c513d","ui_kits/console/Inspector.jsx":"a9892538ef8d","ui_kits/console/Providers.jsx":"0885eff8b2cb","ui_kits/console/RunDetail.jsx":"9205f09587f0","ui_kits/console/Sidebar.jsx":"7507eda2b764","ui_kits/console/app.jsx":"bb380f53bdb0","ui_kits/console/base.jsx":"22ed2571fd21","ui_kits/console/data.js":"8eb6df6a4685","ui_kits/dashboard/app.jsx":"8a05c19a7720","ui_kits/dashboard/data.js":"e9db5219d822"},"inlinedExternals":[],"unexposedExports":[]} */

(() => {

const __ds_ns = (window.ArgusDesignSystem_e0a29b = window.ArgusDesignSystem_e0a29b || {});

const __ds_scope = {};

(__ds_ns.__errors = __ds_ns.__errors || []);

// components/buttons/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Button — primary action control for Argus surfaces.
 * Variants: primary | secondary | ghost | danger. Sizes: sm | md | lg.
 */
function Button({
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
  const cls = ["ds-btn", `ds-btn--${variant}`, size !== "md" && `ds-btn--${size}`, block && "ds-btn--block", loading && "ds-btn--loading", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("button", _extends({
    className: cls,
    disabled: disabled || loading
  }, rest), leftIcon && /*#__PURE__*/React.createElement("span", {
    className: "ds-btn__icon"
  }, leftIcon), children, rightIcon && /*#__PURE__*/React.createElement("span", {
    className: "ds-btn__icon"
  }, rightIcon));
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/buttons/Button.jsx", error: String((e && e.message) || e) }); }

// components/buttons/IconButton.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * IconButton — square, icon-only control for toolbars and dense UI.
 */
function IconButton({
  size = "md",
  variant = "ghost",
  active = false,
  label,
  className = "",
  children,
  ...rest
}) {
  const cls = ["ds-iconbtn", size === "sm" && "ds-iconbtn--sm", variant === "solid" && "ds-iconbtn--solid", active && "ds-iconbtn--active", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("button", _extends({
    className: cls,
    "aria-label": label,
    title: label
  }, rest), children);
}
Object.assign(__ds_scope, { IconButton });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/buttons/IconButton.jsx", error: String((e && e.message) || e) }); }

// components/data/Badge.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Badge — small count or neutral label (notifications, list counts).
 */
function Badge({
  variant = "neutral",
  className = "",
  children,
  ...rest
}) {
  const cls = ["ds-badge", variant !== "neutral" && `ds-badge--${variant}`, className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("span", _extends({
    className: cls
  }, rest), children);
}
Object.assign(__ds_scope, { Badge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/Badge.jsx", error: String((e && e.message) || e) }); }

// components/data/StatusBadge.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const LABELS = {
  pass: "Passed",
  fail: "Failed",
  error: "Error",
  running: "Running",
  skipped: "Skipped",
  flaky: "Flaky"
};

/**
 * StatusBadge — the canonical representation of a test outcome.
 * status: pass | fail | error | running | skipped | flaky
 */
function StatusBadge({
  status = "pass",
  label,
  solid = false,
  className = "",
  ...rest
}) {
  const cls = ["ds-status", `ds-status--${status}`, solid && "ds-status--solid", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("span", _extends({
    className: cls
  }, rest), /*#__PURE__*/React.createElement("span", {
    className: "ds-status__dot"
  }), label || LABELS[status] || status);
}
Object.assign(__ds_scope, { StatusBadge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/StatusBadge.jsx", error: String((e && e.message) || e) }); }

// components/data/Tag.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Tag — monospace chip for adapters, providers, environments and labels.
 * Optional leading dot/icon and a remove affordance.
 */
function Tag({
  icon,
  dotColor,
  onRemove,
  interactive = false,
  className = "",
  children,
  ...rest
}) {
  const cls = ["ds-tag", (interactive || rest.onClick) && "ds-tag--interactive", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("span", _extends({
    className: cls
  }, rest), dotColor && /*#__PURE__*/React.createElement("span", {
    className: "ds-tag__dot",
    style: {
      background: dotColor
    }
  }), icon, children, onRemove && /*#__PURE__*/React.createElement("span", {
    className: "ds-tag__remove",
    role: "button",
    "aria-label": "Remove",
    onClick: e => {
      e.stopPropagation();
      onRemove(e);
    }
  }, /*#__PURE__*/React.createElement("svg", {
    viewBox: "0 0 24 24",
    width: "12",
    height: "12",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "2.5",
    strokeLinecap: "round"
  }, /*#__PURE__*/React.createElement("line", {
    x1: "18",
    y1: "6",
    x2: "6",
    y2: "18"
  }), /*#__PURE__*/React.createElement("line", {
    x1: "6",
    y1: "6",
    x2: "18",
    y2: "18"
  }))));
}
Object.assign(__ds_scope, { Tag });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/data/Tag.jsx", error: String((e && e.message) || e) }); }

// components/feedback/ProgressBar.jsx
try { (() => {
/**
 * ProgressBar — single-value bar, indeterminate "running" shimmer, or a
 * segmented pass/fail/error/skip distribution.
 */
function ProgressBar({
  value = 0,
  segments,
  running = false,
  thin = false,
  className = ""
}) {
  const cls = ["ds-progress", running && "ds-progress--running", thin && "ds-progress--thin", className].filter(Boolean).join(" ");
  if (segments && segments.length) {
    const total = segments.reduce((s, x) => s + x.value, 0) || 1;
    return /*#__PURE__*/React.createElement("div", {
      className: cls,
      role: "progressbar"
    }, segments.map((s, i) => /*#__PURE__*/React.createElement("div", {
      key: i,
      className: `ds-progress__seg ds-progress__seg--${s.kind}`,
      style: {
        width: `${s.value / total * 100}%`
      },
      title: `${s.kind}: ${s.value}`
    })));
  }
  return /*#__PURE__*/React.createElement("div", {
    className: cls,
    role: "progressbar",
    "aria-valuenow": value,
    "aria-valuemin": 0,
    "aria-valuemax": 100
  }, /*#__PURE__*/React.createElement("div", {
    className: "ds-progress__bar",
    style: {
      width: `${running ? 100 : value}%`
    }
  }));
}
Object.assign(__ds_scope, { ProgressBar });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/ProgressBar.jsx", error: String((e && e.message) || e) }); }

// components/feedback/Tooltip.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Tooltip — hover/focus bubble. Wraps a single trigger element.
 */
function Tooltip({
  content,
  kbd,
  children,
  className = "",
  ...rest
}) {
  return /*#__PURE__*/React.createElement("span", _extends({
    className: `ds-tooltip ${className}`
  }, rest), children, /*#__PURE__*/React.createElement("span", {
    className: "ds-tooltip__bubble",
    role: "tooltip"
  }, content, kbd && /*#__PURE__*/React.createElement("span", {
    className: "ds-tooltip__kbd"
  }, kbd)));
}
Object.assign(__ds_scope, { Tooltip });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/feedback/Tooltip.jsx", error: String((e && e.message) || e) }); }

// components/forms/Checkbox.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Checkbox — labelled boolean with a custom check glyph.
 */
function Checkbox({
  checked,
  defaultChecked,
  onChange,
  label,
  disabled = false,
  id,
  ...rest
}) {
  const fieldId = id || React.useId();
  return /*#__PURE__*/React.createElement("label", {
    className: "ds-checkbox",
    htmlFor: fieldId
  }, /*#__PURE__*/React.createElement("input", _extends({
    id: fieldId,
    type: "checkbox",
    checked: checked,
    defaultChecked: defaultChecked,
    onChange: onChange,
    disabled: disabled
  }, rest)), /*#__PURE__*/React.createElement("span", {
    className: "ds-checkbox__box"
  }, /*#__PURE__*/React.createElement("svg", {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "3.5",
    strokeLinecap: "round",
    strokeLinejoin: "round"
  }, /*#__PURE__*/React.createElement("polyline", {
    points: "20 6 9 17 4 12"
  }))), label && /*#__PURE__*/React.createElement("span", {
    className: "ds-checkbox__label"
  }, label));
}
Object.assign(__ds_scope, { Checkbox });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Checkbox.jsx", error: String((e && e.message) || e) }); }

// components/forms/Input.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Input — labelled text field with optional leading icon, hint and error.
 */
function Input({
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
  const inputCls = ["ds-input", mono && "ds-input--mono", error && "ds-input--error", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("div", {
    className: "ds-field"
  }, label && /*#__PURE__*/React.createElement("label", {
    className: "ds-label",
    htmlFor: fieldId
  }, label, required && /*#__PURE__*/React.createElement("span", {
    className: "ds-label__req"
  }, "*")), /*#__PURE__*/React.createElement("div", {
    className: `ds-input-wrap${leadingIcon ? " ds-input-wrap--icon" : ""}`
  }, leadingIcon && /*#__PURE__*/React.createElement("span", {
    className: "ds-input-wrap__icon"
  }, leadingIcon), /*#__PURE__*/React.createElement("input", _extends({
    id: fieldId,
    className: inputCls,
    "aria-invalid": !!error
  }, rest))), (hint || error) && /*#__PURE__*/React.createElement("span", {
    className: `ds-field__hint${error ? " ds-field__hint--error" : ""}`
  }, error || hint));
}
Object.assign(__ds_scope, { Input });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Input.jsx", error: String((e && e.message) || e) }); }

// components/forms/Select.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Select — styled native select with a custom chevron. Pass <option>s as children
 * or an `options` array of { value, label }.
 */
function Select({
  label,
  hint,
  options,
  id,
  className = "",
  children,
  ...rest
}) {
  const fieldId = id || React.useId();
  const select = /*#__PURE__*/React.createElement("div", {
    className: "ds-select-wrap"
  }, /*#__PURE__*/React.createElement("select", _extends({
    id: fieldId,
    className: `ds-select ${className}`
  }, rest), options ? options.map(o => /*#__PURE__*/React.createElement("option", {
    key: o.value,
    value: o.value
  }, o.label)) : children));
  if (!label && !hint) return select;
  return /*#__PURE__*/React.createElement("div", {
    className: "ds-field"
  }, label && /*#__PURE__*/React.createElement("label", {
    className: "ds-label",
    htmlFor: fieldId
  }, label), select, hint && /*#__PURE__*/React.createElement("span", {
    className: "ds-field__hint"
  }, hint));
}
Object.assign(__ds_scope, { Select });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Select.jsx", error: String((e && e.message) || e) }); }

// components/forms/Switch.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Switch — boolean toggle with optional label.
 */
function Switch({
  checked,
  defaultChecked,
  onChange,
  label,
  disabled = false,
  id,
  ...rest
}) {
  const fieldId = id || React.useId();
  return /*#__PURE__*/React.createElement("label", {
    className: "ds-switch",
    htmlFor: fieldId,
    "data-disabled": disabled || undefined
  }, /*#__PURE__*/React.createElement("input", _extends({
    id: fieldId,
    type: "checkbox",
    checked: checked,
    defaultChecked: defaultChecked,
    onChange: onChange,
    disabled: disabled
  }, rest)), /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__track"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__thumb"
  })), label && /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__label"
  }, label));
}
Object.assign(__ds_scope, { Switch });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Switch.jsx", error: String((e && e.message) || e) }); }

// components/surface/Card.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Card — surface container with optional header/footer subcomponents.
 */
function Card({
  raised = false,
  interactive = false,
  className = "",
  children,
  ...rest
}) {
  const cls = ["ds-card", raised && "ds-card--raised", interactive && "ds-card--interactive", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("div", _extends({
    className: cls
  }, rest), children);
}
Card.Header = function CardHeader({
  title,
  actions,
  className = "",
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: `ds-card__header ${className}`
  }, rest), title && /*#__PURE__*/React.createElement("span", {
    className: "ds-card__title"
  }, title), children, actions && /*#__PURE__*/React.createElement("div", {
    style: {
      marginLeft: "auto",
      display: "flex",
      gap: "var(--space-2)"
    }
  }, actions));
};
Card.Body = function CardBody({
  className = "",
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: `ds-card__body ${className}`
  }, rest), children);
};
Card.Footer = function CardFooter({
  className = "",
  children,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    className: `ds-card__footer ${className}`
  }, rest), children);
};
Object.assign(__ds_scope, { Card });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/surface/Card.jsx", error: String((e && e.message) || e) }); }

// components/surface/Tabs.jsx
try { (() => {
/**
 * Tabs — horizontal navigation. Underline (default) or segmented pill style.
 * items: { id, label, icon?, count? }[]
 */
function Tabs({
  items = [],
  value,
  onChange,
  variant = "underline",
  className = ""
}) {
  const wrapCls = ["ds-tabs", variant === "pills" && "ds-tabs--pills", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("div", {
    className: wrapCls,
    role: "tablist"
  }, items.map(it => {
    const active = it.id === value;
    const tabCls = ["ds-tab", variant === "pills" && "ds-tab--pill", active && "ds-tab--active"].filter(Boolean).join(" ");
    return /*#__PURE__*/React.createElement("button", {
      key: it.id,
      className: tabCls,
      role: "tab",
      "aria-selected": active,
      onClick: () => onChange && onChange(it.id)
    }, it.icon, it.label, it.count != null && /*#__PURE__*/React.createElement("span", {
      className: "ds-tab__count"
    }, it.count));
  }));
}
Object.assign(__ds_scope, { Tabs });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/surface/Tabs.jsx", error: String((e && e.message) || e) }); }

// ui_kits/cli/app.jsx
try { (() => {
/* Argus CLI — streaming terminal recreation */

const GLYPH = {
  pass: "✓",
  fail: "✗",
  skip: "⊘",
  run: "●"
};
function Line({
  ln
}) {
  switch (ln.type) {
    case "cmd":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-prompt"
      }, "$"), " ", /*#__PURE__*/React.createElement("span", {
        className: "t-cmd"
      }, hlCmd(ln.text)));
    case "banner":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl t-banner"
      }, ln.text);
    case "info":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "\u2192"), " ", /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, ln.text));
    case "step":
    case "assert":
      {
        const isAssert = ln.type === "assert";
        return /*#__PURE__*/React.createElement("div", {
          className: "tl tl-step"
        }, /*#__PURE__*/React.createElement("span", {
          className: "t-g t-" + ln.status
        }, GLYPH[ln.status]), /*#__PURE__*/React.createElement("span", {
          className: "t-n"
        }, isAssert ? "assert" : ln.n), /*#__PURE__*/React.createElement("span", {
          className: "t-step-text"
        }, isAssert ? /*#__PURE__*/React.createElement("span", {
          className: "t-assert"
        }, ln.text) : ln.text), /*#__PURE__*/React.createElement("span", {
          className: "t-dur"
        }, ln.dur));
      }
    case "note":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl tl-note"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "\u21B3 ", ln.text));
    case "diff":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl tl-diff"
      }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "expected:"), " ", /*#__PURE__*/React.createElement("span", {
        className: "t-str"
      }, ln.expected)), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "actual:\xA0\xA0"), " ", /*#__PURE__*/React.createElement("span", {
        className: "t-fail"
      }, ln.actual)));
    case "blank":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl"
      }, "\xA0");
    case "rule":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl t-rule"
      }, "── " + (ln.text ? ln.text + " " : ""), "".padEnd(ln.text ? 40 - ln.text.length : 46, "─"));
    case "summary":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl tl-summary"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-pass"
      }, ln.pass, " passed"), /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, " \xB7 "), /*#__PURE__*/React.createElement("span", {
        className: "t-fail"
      }, ln.fail, " failed"), /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, " \xB7 "), /*#__PURE__*/React.createElement("span", {
        className: "t-skip"
      }, ln.skip, " skipped"), /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "   \xB7   ", ln.dur));
    case "report":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "report \u2192"), " ", /*#__PURE__*/React.createElement("span", {
        className: "t-link"
      }, ln.text));
    case "exit":
      return /*#__PURE__*/React.createElement("div", {
        className: "tl"
      }, /*#__PURE__*/React.createElement("span", {
        className: "t-dim"
      }, "exit code"), " ", /*#__PURE__*/React.createElement("span", {
        className: ln.code === 0 ? "t-pass" : "t-fail"
      }, ln.code));
    default:
      return null;
  }
}
function hlCmd(text) {
  const parts = text.split(" ");
  return parts.map((p, i) => {
    let cls = "t-arg";
    if (i === 0) cls = "t-bin";else if (i === 1) cls = "t-sub";else if (p.startsWith("--")) cls = "t-flag";else if (p.startsWith(".argus")) cls = "t-path";
    return /*#__PURE__*/React.createElement("span", {
      key: i,
      className: cls
    }, p, i < parts.length - 1 ? " " : "");
  });
}
function Terminal() {
  const {
    TRANSCRIPT
  } = window.CLI_DATA;
  const [shown, setShown] = useState(TRANSCRIPT.length);
  const [playing, setPlaying] = useState(false);
  const bodyRef = useRef(null);
  const timer = useRef(null);
  function play() {
    clearInterval(timer.current);
    setShown(1);
    setPlaying(true);
    let i = 1;
    timer.current = setInterval(() => {
      i += 1;
      setShown(i);
      if (i >= TRANSCRIPT.length) {
        clearInterval(timer.current);
        setPlaying(false);
      }
    }, 230);
  }
  useEffect(() => {
    play();
    return () => clearInterval(timer.current);
  }, []);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [shown]);
  return /*#__PURE__*/React.createElement("div", {
    className: "term"
  }, /*#__PURE__*/React.createElement("div", {
    className: "term__chrome"
  }, /*#__PURE__*/React.createElement("span", {
    className: "term__dots"
  }, /*#__PURE__*/React.createElement("i", null), /*#__PURE__*/React.createElement("i", null), /*#__PURE__*/React.createElement("i", null)), /*#__PURE__*/React.createElement("span", {
    className: "term__title"
  }, "argus \u2014 zsh \u2014 96\xD730"), /*#__PURE__*/React.createElement("button", {
    className: "term__replay",
    onClick: play,
    disabled: playing
  }, /*#__PURE__*/React.createElement(Icon, {
    name: playing ? "loader" : "rotate-ccw",
    size: 13
  }), " ", playing ? "running" : "replay")), /*#__PURE__*/React.createElement("div", {
    className: "term__body",
    ref: bodyRef
  }, TRANSCRIPT.slice(0, shown).map((ln, i) => /*#__PURE__*/React.createElement(Line, {
    key: i,
    ln: ln
  })), playing && /*#__PURE__*/React.createElement("div", {
    className: "tl"
  }, /*#__PURE__*/React.createElement("span", {
    className: "term__cursor"
  })), !playing && /*#__PURE__*/React.createElement("div", {
    className: "tl"
  }, /*#__PURE__*/React.createElement("span", {
    className: "t-prompt"
  }, "$"), " ", /*#__PURE__*/React.createElement("span", {
    className: "term__cursor"
  }))));
}
function CommandRail() {
  const cmds = [{
    c: "argus init",
    d: "scaffold .argus/ + first test"
  }, {
    c: "argus run",
    d: "run a test or suite"
  }, {
    c: "argus watch",
    d: "re-run on file change"
  }, {
    c: "argus record",
    d: "capture steps interactively"
  }, {
    c: "argus report",
    d: "open the HTML report"
  }, {
    c: "argus serve",
    d: "launch the web dashboard"
  }];
  return /*#__PURE__*/React.createElement("aside", {
    className: "rail"
  }, /*#__PURE__*/React.createElement("div", {
    className: "rail__head"
  }, /*#__PURE__*/React.createElement("img", {
    src: "../../assets/logo-mark.svg",
    width: "20",
    height: "20",
    alt: ""
  }), /*#__PURE__*/React.createElement("span", null, "argus"), /*#__PURE__*/React.createElement("span", {
    className: "rail__v"
  }, "v0.4.1")), /*#__PURE__*/React.createElement("p", {
    className: "rail__lead"
  }, "One binary for developers and CI. Exit codes follow test-runner convention: ", /*#__PURE__*/React.createElement("span", {
    className: "mono"
  }, "0"), " pass, ", /*#__PURE__*/React.createElement("span", {
    className: "mono"
  }, "1"), " fail, ", /*#__PURE__*/React.createElement("span", {
    className: "mono"
  }, "2"), " error."), /*#__PURE__*/React.createElement("div", {
    className: "rail__cmds"
  }, cmds.map(x => /*#__PURE__*/React.createElement("div", {
    className: "rail__cmd",
    key: x.c
  }, /*#__PURE__*/React.createElement("code", null, x.c), /*#__PURE__*/React.createElement("span", null, x.d)))), /*#__PURE__*/React.createElement("div", {
    className: "rail__env"
  }, /*#__PURE__*/React.createElement("span", {
    className: "rail__env-title"
  }, "ENV"), /*#__PURE__*/React.createElement("code", null, "ARGUS_PROVIDER=anthropic"), /*#__PURE__*/React.createElement("code", null, "ARGUS_MODEL=claude-3-5-sonnet"), /*#__PURE__*/React.createElement("code", null, "ARGUS_ADAPTER=browser")));
}
function App() {
  useLucide();
  return /*#__PURE__*/React.createElement("div", {
    className: "cli"
  }, /*#__PURE__*/React.createElement(CommandRail, null), /*#__PURE__*/React.createElement("main", {
    className: "cli__main"
  }, /*#__PURE__*/React.createElement(Terminal, null)));
}
ReactDOM.createRoot(document.getElementById("root")).render(/*#__PURE__*/React.createElement(App, null));
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/cli/app.jsx", error: String((e && e.message) || e) }); }

// ui_kits/cli/data.js
try { (() => {
/* Argus CLI — terminal transcript for `argus run` (streamed) */
(function () {
  // type: cmd | banner | info | step | assert | note | diff | blank | rule | summary
  const TRANSCRIPT = [{
    type: "cmd",
    text: "argus run .argus/checkout.test.yaml --adapter browser"
  }, {
    type: "banner",
    text: "◎ Argus v0.4.1 · hybrid-agentic · provider: claude-3-5-sonnet (vision)"
  }, {
    type: "info",
    text: "launching browser · https://shop.example.com"
  }, {
    type: "info",
    text: "observing via screenshot + a11y tree + DOM (CDP)"
  }, {
    type: "blank"
  }, {
    type: "step",
    status: "pass",
    n: "setup",
    text: "Launch browser · dismiss cookie banner",
    dur: "0.62s"
  }, {
    type: "step",
    status: "pass",
    n: "1",
    text: "Log in with test credentials",
    dur: "1.84s"
  }, {
    type: "note",
    text: 'typed test credentials → clicked "Sign in"'
  }, {
    type: "step",
    status: "pass",
    n: "2",
    text: "Add the wireless headphones to the cart",
    dur: "2.10s"
  }, {
    type: "step",
    status: "pass",
    n: "3",
    text: "Open the cart and proceed to checkout",
    dur: "1.27s"
  }, {
    type: "assert",
    status: "pass",
    text: 'visible "Order summary"',
    dur: "0.18s"
  }, {
    type: "step",
    status: "pass",
    n: "5",
    text: "Enter shipping details for the test address",
    dur: "2.46s"
  }, {
    type: "step",
    status: "fail",
    n: "6",
    text: "Apply promo code SAVE10",
    dur: "1.93s"
  }, {
    type: "note",
    text: 'entered "SAVE10" → clicked "Apply"'
  }, {
    type: "assert",
    status: "fail",
    text: 'text_contains "$10.00 off"',
    dur: "0.21s"
  }, {
    type: "diff",
    expected: '"$10.00 off"',
    actual: '"Promo code is not valid for this region."'
  }, {
    type: "step",
    status: "skip",
    n: "td",
    text: "Close browser · capture trace",
    dur: "skipped"
  }, {
    type: "blank"
  }, {
    type: "rule",
    text: "checkout.test.yaml"
  }, {
    type: "summary",
    pass: 6,
    fail: 1,
    skip: 1,
    dur: "10.7s"
  }, {
    type: "report",
    text: ".argus/reports/4821.html"
  }, {
    type: "exit",
    code: 1
  }, {
    type: "rule"
  }];
  window.CLI_DATA = {
    TRANSCRIPT
  };
})();
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/cli/data.js", error: String((e && e.message) || e) }); }

// ui_kits/console/Editor.jsx
try { (() => {
/* Argus Console — Editor view: YAML source + live step preview */

function hl(line, i) {
  const t = line.replace(/\t/g, "  ");
  // comment
  if (/^\s*#/.test(t)) return /*#__PURE__*/React.createElement("span", {
    className: "y-c"
  }, t);
  // key: value  (optionally with leading "- ")
  const m = t.match(/^(\s*)(-\s+)?([A-Za-z0-9_]+)(:)(.*)$/);
  if (m) {
    const [, indent, dash, key, colon, rest] = m;
    return /*#__PURE__*/React.createElement("span", null, indent, dash && /*#__PURE__*/React.createElement("span", {
      className: "y-p"
    }, dash), /*#__PURE__*/React.createElement("span", {
      className: "y-k"
    }, key), /*#__PURE__*/React.createElement("span", {
      className: "y-p"
    }, colon), hlValue(rest));
  }
  // list item: - "string"
  const l = t.match(/^(\s*)(-\s+)(.*)$/);
  if (l) {
    const [, indent, dash, rest] = l;
    return /*#__PURE__*/React.createElement("span", null, indent, /*#__PURE__*/React.createElement("span", {
      className: "y-p"
    }, dash), hlValue(" " + rest).props ? hlValue(rest) : rest);
  }
  return /*#__PURE__*/React.createElement("span", null, t);
}
function hlValue(rest) {
  if (!rest.trim()) return rest;
  const s = rest.match(/^(\s*)("[^"]*"|[^#]*)(\s*#.*)?$/);
  if (!s) return rest;
  const [, sp, val, cmt] = s;
  const isStr = /^".*"$/.test(val.trim());
  const isNum = /^-?\d+(\.\d+)?$/.test(val.trim());
  return /*#__PURE__*/React.createElement("span", null, sp, /*#__PURE__*/React.createElement("span", {
    className: isStr ? "y-s" : isNum ? "y-n" : "y-v"
  }, val), cmt && /*#__PURE__*/React.createElement("span", {
    className: "y-c"
  }, cmt));
}
function Editor() {
  const {
    YAML,
    checkoutSteps
  } = window.ARGUS_DATA;
  const lines = YAML.replace(/\n$/, "").split("\n");
  return /*#__PURE__*/React.createElement("section", {
    className: "editor"
  }, /*#__PURE__*/React.createElement("div", {
    className: "editor__pane editor__pane--code"
  }, /*#__PURE__*/React.createElement("div", {
    className: "pane-head"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "file-code-2",
    size: 15
  }), /*#__PURE__*/React.createElement("span", null, ".argus/checkout.test.yaml"), /*#__PURE__*/React.createElement("span", {
    className: "pane-head__spacer"
  }), /*#__PURE__*/React.createElement(Tag, {
    dotColor: "var(--signal-500)"
  }, "browser"), /*#__PURE__*/React.createElement(IconBtn, {
    name: "wrap-text",
    label: "Wrap",
    size: "sm"
  })), /*#__PURE__*/React.createElement("div", {
    className: "code"
  }, /*#__PURE__*/React.createElement("pre", {
    className: "code__gutter"
  }, lines.map((_, i) => /*#__PURE__*/React.createElement("div", {
    key: i
  }, i + 1))), /*#__PURE__*/React.createElement("pre", {
    className: "code__body"
  }, lines.map((ln, i) => /*#__PURE__*/React.createElement("div", {
    className: "code__line",
    key: i
  }, hl(ln, i)))))), /*#__PURE__*/React.createElement("div", {
    className: "editor__pane editor__pane--preview"
  }, /*#__PURE__*/React.createElement("div", {
    className: "pane-head"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "list-checks",
    size: 15
  }), /*#__PURE__*/React.createElement("span", null, "Resolved steps"), /*#__PURE__*/React.createElement("span", {
    className: "pane-head__spacer"
  }), /*#__PURE__*/React.createElement("span", {
    className: "pane-head__hint"
  }, "hybrid-agentic")), /*#__PURE__*/React.createElement("div", {
    className: "preview"
  }, /*#__PURE__*/React.createElement("div", {
    className: "preview__target"
  }, /*#__PURE__*/React.createElement("div", {
    className: "preview__target-row"
  }, /*#__PURE__*/React.createElement("span", {
    className: "k"
  }, "adapter"), /*#__PURE__*/React.createElement(Tag, {
    dotColor: "var(--signal-500)"
  }, "browser")), /*#__PURE__*/React.createElement("div", {
    className: "preview__target-row"
  }, /*#__PURE__*/React.createElement("span", {
    className: "k"
  }, "launch"), /*#__PURE__*/React.createElement("code", {
    className: "mono"
  }, "https://shop.example.com")), /*#__PURE__*/React.createElement("div", {
    className: "preview__target-row"
  }, /*#__PURE__*/React.createElement("span", {
    className: "k"
  }, "provider"), /*#__PURE__*/React.createElement(Tag, {
    icon: /*#__PURE__*/React.createElement(Icon, {
      name: "sparkles",
      size: 12
    })
  }, "claude-3-5-sonnet"))), /*#__PURE__*/React.createElement("ol", {
    className: "preview__steps"
  }, checkoutSteps.filter(s => s.kind === "step" || s.kind === "assert").map((s, i) => /*#__PURE__*/React.createElement("li", {
    key: s.id,
    className: "pstep pstep--" + s.kind
  }, /*#__PURE__*/React.createElement("span", {
    className: "pstep__n"
  }, i + 1), /*#__PURE__*/React.createElement("span", {
    className: "pstep__icon"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: s.kind === "assert" ? "scan-line" : "mouse-pointer-click",
    size: 13
  })), /*#__PURE__*/React.createElement("span", {
    className: "pstep__text"
  }, s.kind === "assert" ? /*#__PURE__*/React.createElement("code", null, s.text) : s.text), s.kind === "assert" && /*#__PURE__*/React.createElement("span", {
    className: "pstep__badge"
  }, "assert")))))));
}
window.Editor = Editor;
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/Editor.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/Inspector.jsx
try { (() => {
/* Argus Console — right inspector: selected step detail */

function Inspector({
  step
}) {
  if (!step) return /*#__PURE__*/React.createElement("aside", {
    className: "inspector"
  });
  const isFail = step.status === "fail";
  return /*#__PURE__*/React.createElement("aside", {
    className: "inspector"
  }, /*#__PURE__*/React.createElement("div", {
    className: "inspector__head"
  }, /*#__PURE__*/React.createElement("span", {
    className: "inspector__kind"
  }, step.kind), /*#__PURE__*/React.createElement(Status, {
    status: step.status
  })), /*#__PURE__*/React.createElement("div", {
    className: "inspector__title"
  }, step.kind === "assert" ? /*#__PURE__*/React.createElement("code", null, step.text) : step.text), /*#__PURE__*/React.createElement("div", {
    className: "inspector__scroll"
  }, step.action && /*#__PURE__*/React.createElement("div", {
    className: "insp-block"
  }, /*#__PURE__*/React.createElement("div", {
    className: "insp-block__label"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "mouse-pointer-click",
    size: 13
  }), " What Argus did"), /*#__PURE__*/React.createElement("p", {
    className: "insp-block__body"
  }, step.action)), step.reasoning && /*#__PURE__*/React.createElement("div", {
    className: "insp-block"
  }, /*#__PURE__*/React.createElement("div", {
    className: "insp-block__label"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "sparkles",
    size: 13
  }), " Model reasoning"), /*#__PURE__*/React.createElement("p", {
    className: "insp-block__body insp-block__body--quote"
  }, step.reasoning)), step.kind === "assert" && /*#__PURE__*/React.createElement("div", {
    className: "insp-block"
  }, /*#__PURE__*/React.createElement("div", {
    className: "insp-block__label"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "scan-line",
    size: 13
  }), " Assertion"), /*#__PURE__*/React.createElement("div", {
    className: "diff"
  }, /*#__PURE__*/React.createElement("div", {
    className: "diff__row diff__row--exp"
  }, /*#__PURE__*/React.createElement("span", {
    className: "diff__tag"
  }, "expected"), /*#__PURE__*/React.createElement("code", null, step.expected)), /*#__PURE__*/React.createElement("div", {
    className: "diff__row " + (isFail ? "diff__row--act" : "diff__row--ok")
  }, /*#__PURE__*/React.createElement("span", {
    className: "diff__tag"
  }, isFail ? "actual" : "matched"), /*#__PURE__*/React.createElement("code", null, isFail ? step.actual : step.expected)))), /*#__PURE__*/React.createElement("div", {
    className: "insp-block"
  }, /*#__PURE__*/React.createElement("div", {
    className: "insp-block__label"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "terminal",
    size: 13
  }), " Log"), /*#__PURE__*/React.createElement("pre", {
    className: "insp-log"
  }, `[obs] capture screenshot → frame ${step.frame}
[obs] a11y tree · 142 nodes
[act] ${step.kind} · ${step.dur}`, isFail ? `\n[assert] FAILED — expected substring not found\n[exit] 1` : `\n[assert] ok\n[exit] 0`))), /*#__PURE__*/React.createElement("div", {
    className: "inspector__foot"
  }, /*#__PURE__*/React.createElement(Btn, {
    variant: "ghost",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: "copy",
      size: 13
    })
  }, "Copy"), /*#__PURE__*/React.createElement(Btn, {
    variant: "secondary",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: "image",
      size: 13
    })
  }, "Open frame")));
}
window.Inspector = Inspector;
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/Inspector.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/Providers.jsx
try { (() => {
/* Argus Console — Providers view: configure the provider-agnostic layer */

function ProviderCard({
  p,
  active,
  onActivate
}) {
  const connected = p.status === "connected";
  return /*#__PURE__*/React.createElement("div", {
    className: "pcard" + (active ? " is-active" : ""),
    onClick: () => onActivate(p.id)
  }, /*#__PURE__*/React.createElement("div", {
    className: "pcard__top"
  }, /*#__PURE__*/React.createElement("span", {
    className: "pcard__logo"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "sparkles",
    size: 16
  })), /*#__PURE__*/React.createElement("div", {
    className: "pcard__id"
  }, /*#__PURE__*/React.createElement("span", {
    className: "pcard__name"
  }, p.name), /*#__PURE__*/React.createElement("code", {
    className: "pcard__model"
  }, p.model)), /*#__PURE__*/React.createElement("span", {
    className: "pcard__status pcard__status--" + (connected ? "on" : "off")
  }, /*#__PURE__*/React.createElement("span", {
    className: "sdot",
    style: {
      background: connected ? "var(--status-pass)" : "var(--text-faint)"
    }
  }), connected ? "Connected" : "Idle")), /*#__PURE__*/React.createElement("div", {
    className: "pcard__note"
  }, p.note), active && /*#__PURE__*/React.createElement("div", {
    className: "pcard__primary"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "check",
    size: 12
  }), " Active provider"));
}
function Providers() {
  const {
    PROVIDERS
  } = window.ARGUS_DATA;
  const [active, setActive] = useState("anthropic");
  return /*#__PURE__*/React.createElement("section", {
    className: "providers"
  }, /*#__PURE__*/React.createElement("div", {
    className: "providers__head"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("h2", null, "Providers"), /*#__PURE__*/React.createElement("p", {
    className: "providers__sub"
  }, "Argus is provider-agnostic. Pick a default; override per-project or per-test.")), /*#__PURE__*/React.createElement(Btn, {
    variant: "secondary",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: "plus",
      size: 14
    })
  }, "Add provider")), /*#__PURE__*/React.createElement("div", {
    className: "providers__grid"
  }, PROVIDERS.map(p => /*#__PURE__*/React.createElement(ProviderCard, {
    key: p.id,
    p: p,
    active: p.id === active,
    onActivate: setActive
  }))), /*#__PURE__*/React.createElement("div", {
    className: "providers__config"
  }, /*#__PURE__*/React.createElement("div", {
    className: "pane-head"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "sliders-horizontal",
    size: 15
  }), /*#__PURE__*/React.createElement("span", null, "Connection \xB7 ", PROVIDERS.find(p => p.id === active).name)), /*#__PURE__*/React.createElement("div", {
    className: "config-grid"
  }, /*#__PURE__*/React.createElement("label", {
    className: "ds-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-label"
  }, "Model"), /*#__PURE__*/React.createElement("div", {
    className: "ds-select-wrap"
  }, /*#__PURE__*/React.createElement("select", {
    className: "ds-select",
    defaultValue: PROVIDERS.find(p => p.id === active).model
  }, /*#__PURE__*/React.createElement("option", null, PROVIDERS.find(p => p.id === active).model), /*#__PURE__*/React.createElement("option", null, "claude-3-opus")))), /*#__PURE__*/React.createElement("label", {
    className: "ds-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-label"
  }, "API base URL"), /*#__PURE__*/React.createElement("input", {
    className: "ds-input ds-input--mono",
    defaultValue: "https://api.anthropic.com"
  })), /*#__PURE__*/React.createElement("label", {
    className: "ds-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-label"
  }, "API key ", /*#__PURE__*/React.createElement("span", {
    className: "ds-label__req"
  }, "*")), /*#__PURE__*/React.createElement("input", {
    className: "ds-input ds-input--mono",
    type: "password",
    defaultValue: "sk-ant-api03-\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"
  })), /*#__PURE__*/React.createElement("label", {
    className: "ds-field"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-label"
  }, "Vision detail"), /*#__PURE__*/React.createElement("div", {
    className: "ds-select-wrap"
  }, /*#__PURE__*/React.createElement("select", {
    className: "ds-select",
    defaultValue: "auto"
  }, /*#__PURE__*/React.createElement("option", null, "auto"), /*#__PURE__*/React.createElement("option", null, "high"), /*#__PURE__*/React.createElement("option", null, "low"))))), /*#__PURE__*/React.createElement("div", {
    className: "config-foot"
  }, /*#__PURE__*/React.createElement("label", {
    className: "ds-switch"
  }, /*#__PURE__*/React.createElement("input", {
    type: "checkbox",
    defaultChecked: true
  }), /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__track"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__thumb"
  })), /*#__PURE__*/React.createElement("span", {
    className: "ds-switch__label"
  }, "Use for all projects")), /*#__PURE__*/React.createElement("div", {
    className: "config-foot__actions"
  }, /*#__PURE__*/React.createElement(Btn, {
    variant: "ghost",
    size: "sm"
  }, "Test connection"), /*#__PURE__*/React.createElement(Btn, {
    variant: "primary",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: "check",
      size: 14
    })
  }, "Save")))));
}
window.Providers = Providers;
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/Providers.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/RunDetail.jsx
try { (() => {
/* Argus Console — center stage for the Runs view */

function RunHeader({
  test,
  running,
  onRun
}) {
  const {
    ADAPTERS
  } = window.ARGUS_DATA;
  const ad = ADAPTERS[test.adapter];
  const passed = test.passed,
    total = test.total;
  const failed = total - passed - (test.status === "running" ? total - passed : 0);
  const segs = [{
    kind: "pass",
    value: passed
  }, {
    kind: "fail",
    value: test.status === "fail" ? 1 : 0
  }, {
    kind: "skip",
    value: Math.max(0, total - passed - (test.status === "fail" ? 1 : 0))
  }];
  return /*#__PURE__*/React.createElement("div", {
    className: "run-head"
  }, /*#__PURE__*/React.createElement("div", {
    className: "run-head__top"
  }, /*#__PURE__*/React.createElement("div", {
    className: "run-head__title"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "file-code-2",
    size: 18
  }), /*#__PURE__*/React.createElement("h2", null, test.name), running ? /*#__PURE__*/React.createElement(Status, {
    status: "running"
  }) : /*#__PURE__*/React.createElement(Status, {
    status: test.status
  })), /*#__PURE__*/React.createElement("div", {
    className: "run-head__actions"
  }, /*#__PURE__*/React.createElement(IconBtn, {
    name: "history",
    label: "Run history"
  }), /*#__PURE__*/React.createElement(IconBtn, {
    name: "more-horizontal",
    label: "More"
  }), /*#__PURE__*/React.createElement(Btn, {
    variant: running ? "secondary" : "primary",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: running ? "loader" : "play",
      size: 14
    }),
    onClick: onRun
  }, running ? "Running…" : "Re-run"))), /*#__PURE__*/React.createElement("div", {
    className: "run-head__meta"
  }, /*#__PURE__*/React.createElement(Tag, {
    dotColor: ad.color
  }, ad.label), /*#__PURE__*/React.createElement(Tag, {
    icon: /*#__PURE__*/React.createElement(Icon, {
      name: "sparkles",
      size: 12
    })
  }, test.provider), /*#__PURE__*/React.createElement("span", {
    className: "run-head__stat"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "check",
    size: 13
  }), " ", passed, "/", total, " steps"), /*#__PURE__*/React.createElement("span", {
    className: "run-head__stat"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "clock",
    size: 13
  }), " ", test.dur), /*#__PURE__*/React.createElement("span", {
    className: "run-head__stat run-head__stat--muted"
  }, "exit ", test.status === "pass" ? "0" : test.status === "fail" ? "1" : "—")), /*#__PURE__*/React.createElement("div", {
    className: "run-head__bar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "ds-progress"
  }, segs.map((s, i) => s.value > 0 ? /*#__PURE__*/React.createElement("div", {
    key: i,
    className: "ds-progress__seg ds-progress__seg--" + s.kind,
    style: {
      width: s.value / total * 100 + "%"
    }
  }) : null))));
}
const KIND_GLYPH = {
  setup: "settings-2",
  teardown: "power",
  assert: "scan-line",
  step: "mouse-pointer-click"
};
function StepRow({
  step,
  n,
  active,
  onClick
}) {
  return /*#__PURE__*/React.createElement("button", {
    className: "step" + (active ? " is-active" : "") + (" step--" + step.status),
    onClick: onClick
  }, /*#__PURE__*/React.createElement("span", {
    className: "step__n"
  }, String(n).padStart(2, "0")), /*#__PURE__*/React.createElement("span", {
    className: "sdot sdot--" + step.status
  }), /*#__PURE__*/React.createElement("span", {
    className: "step__icon"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: KIND_GLYPH[step.kind],
    size: 14
  })), /*#__PURE__*/React.createElement("span", {
    className: "step__text"
  }, step.kind === "assert" ? /*#__PURE__*/React.createElement("code", {
    className: "step__assert"
  }, step.text) : step.text, step.kind !== "step" && step.kind !== "assert" && /*#__PURE__*/React.createElement("span", {
    className: "step__kind"
  }, step.kind)), /*#__PURE__*/React.createElement("span", {
    className: "step__dur"
  }, step.dur));
}
function RunDetail({
  test,
  steps,
  selectedStepId,
  onSelectStep,
  running,
  onRun
}) {
  const sel = steps.find(s => s.id === selectedStepId) || steps[0];
  return /*#__PURE__*/React.createElement("section", {
    className: "run"
  }, /*#__PURE__*/React.createElement(RunHeader, {
    test: test,
    running: running,
    onRun: onRun
  }), /*#__PURE__*/React.createElement("div", {
    className: "run__stage"
  }, /*#__PURE__*/React.createElement(CapturedFrame, {
    big: true,
    status: sel.status === "fail" ? "fail" : "pass",
    label: sel.kind === "assert" ? sel.expected : "target",
    caption: `frame ${sel.frame} · 1280×800 · ${sel.kind}`
  }), /*#__PURE__*/React.createElement("div", {
    className: "filmstrip"
  }, steps.map((s, i) => /*#__PURE__*/React.createElement("button", {
    key: s.id,
    className: "filmstrip__item" + (s.id === sel.id ? " is-active" : ""),
    onClick: () => onSelectStep(s.id),
    title: s.text
  }, /*#__PURE__*/React.createElement("span", {
    className: "filmstrip__thumb"
  }, /*#__PURE__*/React.createElement("span", {
    className: "filmstrip__badge sdot--" + s.status
  }), /*#__PURE__*/React.createElement("span", {
    className: "filmstrip__frame"
  }, s.frame)))))), /*#__PURE__*/React.createElement("div", {
    className: "run__steps"
  }, /*#__PURE__*/React.createElement("div", {
    className: "run__steps-head"
  }, /*#__PURE__*/React.createElement("span", null, "Step timeline"), /*#__PURE__*/React.createElement("span", {
    className: "run__steps-count"
  }, steps.length, " steps")), steps.map((s, i) => /*#__PURE__*/React.createElement(StepRow, {
    key: s.id,
    step: s,
    n: i,
    active: s.id === sel.id,
    onClick: () => onSelectStep(s.id)
  }))));
}
window.RunDetail = RunDetail;
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/RunDetail.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/Sidebar.jsx
try { (() => {
/* Argus Console — left sidebar: project + test tree */

function Sidebar({
  suites,
  activeTestId,
  onSelectTest,
  query,
  setQuery
}) {
  const {
    ADAPTERS
  } = window.ARGUS_DATA;
  return /*#__PURE__*/React.createElement("aside", {
    className: "sidebar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "sidebar__search"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "search",
    size: 15
  }), /*#__PURE__*/React.createElement("input", {
    className: "sidebar__input",
    placeholder: "Search tests\u2026",
    value: query,
    onChange: e => setQuery(e.target.value)
  }), /*#__PURE__*/React.createElement("kbd", {
    className: "sidebar__kbd"
  }, "\u2318K")), /*#__PURE__*/React.createElement("div", {
    className: "sidebar__scroll"
  }, /*#__PURE__*/React.createElement("div", {
    className: "tree"
  }, suites.map(suite => {
    const tests = suite.tests.filter(t => t.name.toLowerCase().includes(query.toLowerCase()));
    if (!tests.length) return null;
    return /*#__PURE__*/React.createElement("div", {
      className: "tree__group",
      key: suite.name
    }, /*#__PURE__*/React.createElement("div", {
      className: "tree__suite"
    }, /*#__PURE__*/React.createElement(Icon, {
      name: "chevron-down",
      size: 13
    }), /*#__PURE__*/React.createElement(Icon, {
      name: "folder",
      size: 14
    }), /*#__PURE__*/React.createElement("span", null, suite.name), /*#__PURE__*/React.createElement("span", {
      className: "tree__count"
    }, suite.tests.length)), tests.map(t => /*#__PURE__*/React.createElement("button", {
      key: t.id,
      className: "tree__test" + (t.id === activeTestId ? " is-active" : ""),
      onClick: () => onSelectTest(t.id)
    }, /*#__PURE__*/React.createElement("span", {
      className: "sdot sdot--" + t.status
    }), /*#__PURE__*/React.createElement("span", {
      className: "tree__name"
    }, t.name), t.flaky && /*#__PURE__*/React.createElement(Icon, {
      name: "zap",
      size: 12,
      className: "tree__flaky"
    }), /*#__PURE__*/React.createElement("span", {
      className: "tree__dur"
    }, t.dur))));
  }))), /*#__PURE__*/React.createElement("div", {
    className: "sidebar__legend"
  }, /*#__PURE__*/React.createElement("span", {
    className: "legend__title"
  }, "Adapters"), /*#__PURE__*/React.createElement("div", {
    className: "legend__chips"
  }, Object.entries(ADAPTERS).map(([k, v]) => /*#__PURE__*/React.createElement("span", {
    className: "legend__chip",
    key: k
  }, /*#__PURE__*/React.createElement("span", {
    className: "legend__dot",
    style: {
      background: v.color
    }
  }), v.label)))));
}
window.Sidebar = Sidebar;
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/Sidebar.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/app.jsx
try { (() => {
/* Argus Console — app shell, titlebar, status bar, view switching */

function TitleBar({
  view,
  setView,
  running,
  onRunAll,
  watch,
  setWatch
}) {
  return /*#__PURE__*/React.createElement("header", {
    className: "titlebar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "titlebar__left"
  }, /*#__PURE__*/React.createElement("span", {
    className: "win-dots"
  }, /*#__PURE__*/React.createElement("i", null), /*#__PURE__*/React.createElement("i", null), /*#__PURE__*/React.createElement("i", null)), /*#__PURE__*/React.createElement("span", {
    className: "brand"
  }, /*#__PURE__*/React.createElement("img", {
    src: "../../assets/logo-mark.svg",
    width: "18",
    height: "18",
    alt: ""
  }), /*#__PURE__*/React.createElement("span", {
    className: "brand__name"
  }, "Argus")), /*#__PURE__*/React.createElement("span", {
    className: "proj"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "git-branch",
    size: 13
  }), "shop-frontend", /*#__PURE__*/React.createElement(Icon, {
    name: "chevron-down",
    size: 13
  }))), /*#__PURE__*/React.createElement("nav", {
    className: "viewnav"
  }, [{
    id: "runs",
    label: "Runs",
    icon: "layout-list"
  }, {
    id: "editor",
    label: "Editor",
    icon: "file-code-2"
  }, {
    id: "providers",
    label: "Providers",
    icon: "sparkles"
  }].map(v => /*#__PURE__*/React.createElement("button", {
    key: v.id,
    className: "viewnav__tab" + (view === v.id ? " is-active" : ""),
    onClick: () => setView(v.id)
  }, /*#__PURE__*/React.createElement(Icon, {
    name: v.icon,
    size: 15
  }), v.label))), /*#__PURE__*/React.createElement("div", {
    className: "titlebar__right"
  }, /*#__PURE__*/React.createElement("button", {
    className: "watch" + (watch ? " is-on" : ""),
    onClick: () => setWatch(!watch),
    title: "Watch mode"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "eye",
    size: 14
  }), "watch"), /*#__PURE__*/React.createElement(Tag, {
    icon: /*#__PURE__*/React.createElement(Icon, {
      name: "sparkles",
      size: 12
    })
  }, "claude-3-5-sonnet"), /*#__PURE__*/React.createElement(Btn, {
    variant: "primary",
    size: "sm",
    leftIcon: /*#__PURE__*/React.createElement(Icon, {
      name: running ? "loader" : "play",
      size: 14
    }),
    onClick: onRunAll
  }, running ? "Running" : "Run all")));
}
function StatusBar({
  test,
  running
}) {
  return /*#__PURE__*/React.createElement("footer", {
    className: "statusbar"
  }, /*#__PURE__*/React.createElement("div", {
    className: "statusbar__group"
  }, /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "git-branch",
    size: 12
  }), " main"), /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item statusbar__item--ok"
  }, /*#__PURE__*/React.createElement("span", {
    className: "sdot sdot--pass"
  }), " connected"), /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "cpu",
    size: 12
  }), " ollama \xB7 localhost:11434")), /*#__PURE__*/React.createElement("div", {
    className: "statusbar__group"
  }, /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item"
  }, running ? "running checkout.test.yaml…" : `${test.passed}/${test.total} steps`), /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item " + (test.status === "fail" ? "statusbar__item--fail" : "statusbar__item--ok")
  }, "exit ", test.status === "pass" ? 0 : test.status === "fail" ? 1 : "—"), /*#__PURE__*/React.createElement("span", {
    className: "statusbar__item statusbar__item--muted"
  }, "argus v0.4.1")));
}
function App() {
  useLucide();
  const {
    SUITES
  } = window.ARGUS_DATA;
  const allTests = SUITES.flatMap(s => s.tests);
  const [view, setView] = useState("runs");
  const [activeTestId, setActiveTestId] = useState("t1");
  const [selectedStepId, setSelectedStepId] = useState("s6");
  const [query, setQuery] = useState("");
  const [running, setRunning] = useState(false);
  const [watch, setWatch] = useState(true);
  const test = allTests.find(t => t.id === activeTestId) || allTests[0];
  const steps = test.steps || window.ARGUS_DATA.checkoutSteps;
  function runOnce() {
    setRunning(true);
    setTimeout(() => setRunning(false), 1800);
  }
  function selectTest(id) {
    setActiveTestId(id);
    setView("runs");
    const t = allTests.find(x => x.id === id);
    if (t && t.steps) setSelectedStepId(t.steps.find(s => s.status === "fail")?.id || t.steps[0].id);
  }
  return /*#__PURE__*/React.createElement("div", {
    className: "ds-app"
  }, /*#__PURE__*/React.createElement(TitleBar, {
    view: view,
    setView: setView,
    running: running,
    onRunAll: runOnce,
    watch: watch,
    setWatch: setWatch
  }), /*#__PURE__*/React.createElement("div", {
    className: "workbench"
  }, /*#__PURE__*/React.createElement(Sidebar, {
    suites: SUITES,
    activeTestId: activeTestId,
    onSelectTest: selectTest,
    query: query,
    setQuery: setQuery
  }), /*#__PURE__*/React.createElement("main", {
    className: "main"
  }, view === "runs" && /*#__PURE__*/React.createElement(RunDetail, {
    test: test,
    steps: steps,
    selectedStepId: selectedStepId,
    onSelectStep: setSelectedStepId,
    running: running,
    onRun: runOnce
  }), view === "editor" && /*#__PURE__*/React.createElement(Editor, null), view === "providers" && /*#__PURE__*/React.createElement(Providers, null)), view === "runs" && /*#__PURE__*/React.createElement(Inspector, {
    step: steps.find(s => s.id === selectedStepId) || steps[0]
  })), /*#__PURE__*/React.createElement(StatusBar, {
    test: test,
    running: running
  }));
}
ReactDOM.createRoot(document.getElementById("root")).render(/*#__PURE__*/React.createElement(App, null));
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/app.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/base.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/* Argus Console — shared atoms (Lucide icon + thin wrappers over the DS .ds-* classes) */

const {
  useState,
  useEffect,
  useRef
} = React;

/* Lucide icon. Renders a placeholder <i> that lucide.createIcons() swaps for an <svg>.
   Size is controlled by font-size (CSS rule `.lucide{width:1em;height:1em}`). */
function Icon({
  name,
  size = 16,
  sw = 2,
  className = "",
  style = {}
}) {
  return /*#__PURE__*/React.createElement("i", {
    "data-lucide": name,
    "data-sw": sw,
    className: "ic " + className,
    style: {
      fontSize: size,
      width: size,
      height: size,
      ...style
    }
  });
}

/* Call after every render so freshly-mounted <i data-lucide> get replaced. */
function useLucide() {
  useEffect(() => {
    if (window.lucide) {
      window.lucide.createIcons({
        attrs: {
          "stroke-width": 1.9
        }
      });
    }
  });
}
const STATUS_LABEL = {
  pass: "Passed",
  fail: "Failed",
  error: "Error",
  running: "Running",
  skipped: "Skipped",
  flaky: "Flaky",
  pending: "Pending"
};
function Status({
  status,
  label,
  solid
}) {
  const cls = ["ds-status", `ds-status--${status === "pending" ? "skipped" : status}`, solid && "ds-status--solid"].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("span", {
    className: cls
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-status__dot"
  }), label || STATUS_LABEL[status] || status);
}
function Tag({
  children,
  dotColor,
  icon,
  onClick,
  className = ""
}) {
  return /*#__PURE__*/React.createElement("span", {
    className: "ds-tag " + (onClick ? "ds-tag--interactive " : "") + className,
    onClick: onClick
  }, dotColor && /*#__PURE__*/React.createElement("span", {
    className: "ds-tag__dot",
    style: {
      background: dotColor
    }
  }), icon, children);
}
function Btn({
  variant = "primary",
  size,
  leftIcon,
  children,
  className = "",
  ...rest
}) {
  const cls = ["ds-btn", `ds-btn--${variant}`, size && `ds-btn--${size}`, className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("button", _extends({
    className: cls
  }, rest), leftIcon && /*#__PURE__*/React.createElement("span", {
    className: "ds-btn__icon"
  }, leftIcon), children);
}
function IconBtn({
  name,
  label,
  active,
  variant,
  size,
  onClick
}) {
  const cls = ["ds-iconbtn", variant === "solid" && "ds-iconbtn--solid", size === "sm" && "ds-iconbtn--sm", active && "ds-iconbtn--active"].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("button", {
    className: cls,
    "aria-label": label,
    title: label,
    onClick: onClick
  }, /*#__PURE__*/React.createElement(Icon, {
    name: name,
    size: size === "sm" ? 15 : 17
  }));
}

/* A small "captured frame" — abstract wireframe of the app under test with an
   annotation overlay (what Argus is looking at). No real product UI is fabricated. */
function CapturedFrame({
  status = "pass",
  label = "Order summary",
  caption,
  big
}) {
  const ring = status === "fail" ? "var(--status-fail)" : status === "running" ? "var(--status-running)" : "var(--status-pass)";
  return /*#__PURE__*/React.createElement("div", {
    className: "frame" + (big ? " frame--big" : "")
  }, /*#__PURE__*/React.createElement("div", {
    className: "frame__chrome"
  }, /*#__PURE__*/React.createElement("span", {
    className: "frame__dot"
  }), /*#__PURE__*/React.createElement("span", {
    className: "frame__dot"
  }), /*#__PURE__*/React.createElement("span", {
    className: "frame__dot"
  }), /*#__PURE__*/React.createElement("span", {
    className: "frame__url"
  }, "shop.example.com")), /*#__PURE__*/React.createElement("div", {
    className: "frame__body"
  }, /*#__PURE__*/React.createElement("div", {
    className: "wf wf--bar"
  }), /*#__PURE__*/React.createElement("div", {
    className: "wf-row"
  }, /*#__PURE__*/React.createElement("div", {
    className: "wf wf--side"
  }), /*#__PURE__*/React.createElement("div", {
    className: "wf-main"
  }, /*#__PURE__*/React.createElement("div", {
    className: "wf wf--h"
  }), /*#__PURE__*/React.createElement("div", {
    className: "wf wf--p"
  }), /*#__PURE__*/React.createElement("div", {
    className: "wf wf--p short"
  }), /*#__PURE__*/React.createElement("div", {
    className: "frame__target",
    style: {
      borderColor: ring,
      boxShadow: `0 0 0 3px ${ring}22`
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "frame__target-label",
    style: {
      background: ring
    }
  }, label), /*#__PURE__*/React.createElement("div", {
    className: "wf wf--btn"
  }))))), caption && /*#__PURE__*/React.createElement("div", {
    className: "frame__caption"
  }, caption), /*#__PURE__*/React.createElement("span", {
    className: "frame__reticle frame__reticle--tl"
  }), /*#__PURE__*/React.createElement("span", {
    className: "frame__reticle frame__reticle--br"
  }));
}
Object.assign(window, {
  Icon,
  useLucide,
  Status,
  Tag,
  Btn,
  IconBtn,
  CapturedFrame,
  STATUS_LABEL
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/base.jsx", error: String((e && e.message) || e) }); }

// ui_kits/console/data.js
try { (() => {
/* Argus Console — demo data (fake, for the UI kit recreation) */
(function () {
  const ADAPTERS = {
    browser: {
      label: "browser",
      color: "var(--signal-500)"
    },
    "desktop-gui": {
      label: "desktop-gui",
      color: "var(--beacon-500)"
    },
    cli: {
      label: "cli",
      color: "var(--violet-500)"
    },
    tui: {
      label: "tui",
      color: "var(--blue-500)"
    },
    electron: {
      label: "electron",
      color: "var(--green-500)"
    }
  };
  const PROVIDERS = [{
    id: "anthropic",
    name: "Anthropic",
    model: "claude-3-5-sonnet",
    status: "connected",
    note: "Vision · primary"
  }, {
    id: "openai",
    name: "OpenAI",
    model: "gpt-4o",
    status: "connected",
    note: "Vision"
  }, {
    id: "ollama",
    name: "Ollama",
    model: "llava:13b",
    status: "connected",
    note: "Local · localhost:11434"
  }, {
    id: "gemini",
    name: "Google",
    model: "gemini-2.0-flash",
    status: "idle",
    note: "Vision"
  }, {
    id: "azure",
    name: "Azure",
    model: "gpt-4o",
    status: "idle",
    note: "eastus2"
  }, {
    id: "litellm",
    name: "LiteLLM",
    model: "proxy",
    status: "idle",
    note: "catch-all"
  }];

  // The detailed run shown in the center stage
  const checkoutSteps = [{
    id: "s0",
    kind: "setup",
    text: "Launch browser · https://shop.example.com",
    status: "pass",
    dur: "0.62s",
    action: "navigated to target URL, waited for network idle",
    frame: "01"
  }, {
    id: "s1",
    kind: "step",
    text: "Log in with test credentials",
    status: "pass",
    dur: "1.84s",
    action: "typed user 'qa@example.com', filled password field, clicked “Sign in”",
    reasoning: "Resolved the login form by its labelled fields; submit button matched by accessible name.",
    frame: "02"
  }, {
    id: "s2",
    kind: "step",
    text: "Add the wireless headphones to the cart",
    status: "pass",
    dur: "2.10s",
    action: "searched 'wireless headphones', opened first result, clicked “Add to cart”",
    reasoning: "Product grid had 12 results; selected the first matching card.",
    frame: "03"
  }, {
    id: "s3",
    kind: "step",
    text: "Open the cart and proceed to checkout",
    status: "pass",
    dur: "1.27s",
    action: "clicked cart icon, clicked “Checkout”",
    frame: "04"
  }, {
    id: "s4",
    kind: "assert",
    text: "visible: \"Order summary\"",
    status: "pass",
    dur: "0.18s",
    assertion: "visible",
    expected: "Order summary",
    frame: "05"
  }, {
    id: "s5",
    kind: "step",
    text: "Enter shipping details for the test address",
    status: "pass",
    dur: "2.46s",
    action: "filled name, address, city, ZIP from env fixtures",
    frame: "06"
  }, {
    id: "s6",
    kind: "step",
    text: "Apply promo code SAVE10",
    status: "fail",
    dur: "1.93s",
    action: "entered 'SAVE10' into promo field, clicked “Apply”",
    reasoning: "The applied discount did not appear in the order total.",
    frame: "07"
  }, {
    id: "s7",
    kind: "assert",
    text: "text_contains: \"$10.00 off\"",
    status: "fail",
    dur: "0.21s",
    assertion: "text_contains",
    expected: "$10.00 off",
    actual: "Promo code is not valid for this region.",
    frame: "07"
  }, {
    id: "s8",
    kind: "teardown",
    text: "Close browser · capture trace",
    status: "skipped",
    dur: "—",
    action: "skipped after failure (continue_on_failure: false)",
    frame: "—"
  }];
  const SUITES = [{
    name: "checkout",
    tests: [{
      id: "t1",
      name: "checkout.test.yaml",
      adapter: "browser",
      provider: "gemini-2.0-flash",
      status: "fail",
      dur: "10.7s",
      steps: checkoutSteps,
      passed: 6,
      total: 9,
      flaky: false,
      active: true
    }, {
      id: "t2",
      name: "guest-checkout.test.yaml",
      adapter: "browser",
      provider: "gpt-4o",
      status: "pass",
      dur: "8.4s",
      passed: 7,
      total: 7,
      flaky: false
    }, {
      id: "t3",
      name: "cart-persistence.test.yaml",
      adapter: "browser",
      provider: "claude-3-5-sonnet",
      status: "flaky",
      dur: "9.1s",
      passed: 6,
      total: 7,
      flaky: true
    }]
  }, {
    name: "desktop",
    tests: [{
      id: "t4",
      name: "settings-dialog.test.yaml",
      adapter: "desktop-gui",
      provider: "claude-3-5-sonnet",
      status: "pass",
      dur: "5.2s",
      passed: 5,
      total: 5
    }, {
      id: "t5",
      name: "file-export.test.yaml",
      adapter: "electron",
      provider: "gpt-4o",
      status: "pass",
      dur: "6.8s",
      passed: 8,
      total: 8
    }]
  }, {
    name: "cli",
    tests: [{
      id: "t6",
      name: "init-wizard.test.yaml",
      adapter: "cli",
      provider: "llava:13b",
      status: "pass",
      dur: "1.1s",
      passed: 4,
      total: 4
    }, {
      id: "t7",
      name: "migrate.test.yaml",
      adapter: "tui",
      provider: "claude-3-5-sonnet",
      status: "running",
      dur: "—",
      passed: 2,
      total: 6
    }]
  }];
  const YAML = `# .argus/checkout.test.yaml
name: Checkout with promo code
target:
  adapter: browser
  launch: "https://shop.example.com"

setup:
  - "Launch browser and dismiss cookie banner"

env:
  address: fixtures/test-address.json

steps:
  - "Log in with test credentials"
  - "Add the wireless headphones to the cart"
  - "Open the cart and proceed to checkout"
  - assert:
      visible: "Order summary"
  - "Enter shipping details for the test address"
  - "Apply promo code SAVE10"
  - assert:
      text_contains: "$10.00 off"

teardown:
  - "Close browser and capture trace"
`;
  window.ARGUS_DATA = {
    ADAPTERS,
    PROVIDERS,
    SUITES,
    YAML,
    checkoutSteps
  };
})();
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/console/data.js", error: String((e && e.message) || e) }); }

// ui_kits/dashboard/app.jsx
try { (() => {
/* Argus Dashboard — self-hosted web dashboard (single screen) */

function TopNav({
  tab,
  setTab
}) {
  return /*#__PURE__*/React.createElement("header", {
    className: "dnav"
  }, /*#__PURE__*/React.createElement("div", {
    className: "dnav__brand"
  }, /*#__PURE__*/React.createElement("img", {
    src: "../../assets/logo-mark.svg",
    width: "22",
    height: "22",
    alt: ""
  }), /*#__PURE__*/React.createElement("span", {
    className: "dnav__name"
  }, "Argus"), /*#__PURE__*/React.createElement("span", {
    className: "dnav__sub"
  }, "dashboard")), /*#__PURE__*/React.createElement("nav", {
    className: "dnav__tabs"
  }, ["Overview", "Runs", "Tests", "Flakiness"].map(t => /*#__PURE__*/React.createElement("button", {
    key: t,
    className: "dnav__tab" + (tab === t ? " is-active" : ""),
    onClick: () => setTab(t)
  }, t))), /*#__PURE__*/React.createElement("div", {
    className: "dnav__right"
  }, /*#__PURE__*/React.createElement("span", {
    className: "dnav__search"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "search",
    size: 14
  }), " Search runs\u2026"), /*#__PURE__*/React.createElement("span", {
    className: "dnav__env"
  }, /*#__PURE__*/React.createElement("span", {
    className: "sdot sdot--pass"
  }), " staging ", /*#__PURE__*/React.createElement(Icon, {
    name: "chevron-down",
    size: 13
  })), /*#__PURE__*/React.createElement("span", {
    className: "dnav__avatar"
  }, "QA")));
}
function KpiCard({
  k
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, /*#__PURE__*/React.createElement("div", {
    className: "kpi__top"
  }, /*#__PURE__*/React.createElement("span", {
    className: "kpi__icon"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: k.icon,
    size: 15
  })), /*#__PURE__*/React.createElement("span", {
    className: "kpi__label"
  }, k.label)), /*#__PURE__*/React.createElement("div", {
    className: "kpi__value"
  }, k.value), /*#__PURE__*/React.createElement("div", {
    className: "kpi__delta " + (k.up ? "kpi__delta--up" : "kpi__delta--down")
  }, /*#__PURE__*/React.createElement(Icon, {
    name: k.up ? "trending-up" : "trending-down",
    size: 13
  }), " ", k.delta, /*#__PURE__*/React.createElement("span", {
    className: "kpi__delta-note"
  }, "vs last week")));
}
function TrendChart({
  trend
}) {
  const max = Math.max(...trend.map(d => d.pass + d.fail));
  return /*#__PURE__*/React.createElement("div", {
    className: "card-panel trend"
  }, /*#__PURE__*/React.createElement("div", {
    className: "panel-head"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "panel-title"
  }, "Pass / fail trend"), /*#__PURE__*/React.createElement("span", {
    className: "panel-sub"
  }, "last 14 days")), /*#__PURE__*/React.createElement("div", {
    className: "trend__legend"
  }, /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
    className: "lg lg--pass"
  }), " pass"), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement("span", {
    className: "lg lg--fail"
  }), " fail"))), /*#__PURE__*/React.createElement("div", {
    className: "trend__plot"
  }, [100, 75, 50, 25, 0].map(g => /*#__PURE__*/React.createElement("div", {
    className: "trend__grid",
    key: g,
    style: {
      bottom: g + "%"
    }
  })), /*#__PURE__*/React.createElement("div", {
    className: "trend__bars"
  }, trend.map((d, i) => {
    const total = d.pass + d.fail;
    return /*#__PURE__*/React.createElement("div", {
      className: "trend__col",
      key: i,
      title: `${d.pass} pass · ${d.fail} fail`
    }, /*#__PURE__*/React.createElement("div", {
      className: "trend__stack",
      style: {
        height: total / max * 100 + "%"
      }
    }, /*#__PURE__*/React.createElement("div", {
      className: "trend__fail",
      style: {
        height: d.fail / total * 100 + "%"
      }
    }), /*#__PURE__*/React.createElement("div", {
      className: "trend__pass",
      style: {
        height: d.pass / total * 100 + "%"
      }
    })));
  }))));
}
function Sparkline({
  runs
}) {
  return /*#__PURE__*/React.createElement("span", {
    className: "spark"
  }, runs.map((s, i) => /*#__PURE__*/React.createElement("span", {
    key: i,
    className: "spark__c spark__c--" + s
  })));
}
function FlakyTable({
  flaky,
  adapters
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "card-panel"
  }, /*#__PURE__*/React.createElement("div", {
    className: "panel-head"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "panel-title"
  }, "Flakiness"), /*#__PURE__*/React.createElement("span", {
    className: "panel-sub"
  }, "tests with non-deterministic outcomes")), /*#__PURE__*/React.createElement("span", {
    className: "panel-link"
  }, "View all ", /*#__PURE__*/React.createElement(Icon, {
    name: "arrow-right",
    size: 13
  }))), /*#__PURE__*/React.createElement("div", {
    className: "ftable"
  }, flaky.map(f => /*#__PURE__*/React.createElement("div", {
    className: "frow",
    key: f.name
  }, /*#__PURE__*/React.createElement("span", {
    className: "frow__dot",
    style: {
      background: adapters[f.adapter]
    }
  }), /*#__PURE__*/React.createElement("span", {
    className: "frow__name"
  }, f.name), /*#__PURE__*/React.createElement(Sparkline, {
    runs: f.runs
  }), /*#__PURE__*/React.createElement("span", {
    className: "frow__score"
  }, /*#__PURE__*/React.createElement("span", {
    className: "frow__bar"
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      width: f.score * 100 + "%"
    }
  })), /*#__PURE__*/React.createElement("span", {
    className: "frow__pct"
  }, Math.round(f.score * 100), "%"))))));
}
function RunsTable({
  runs,
  adapters
}) {
  return /*#__PURE__*/React.createElement("div", {
    className: "card-panel"
  }, /*#__PURE__*/React.createElement("div", {
    className: "panel-head"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("span", {
    className: "panel-title"
  }, "Recent runs"), /*#__PURE__*/React.createElement("span", {
    className: "panel-sub"
  }, "across all adapters")), /*#__PURE__*/React.createElement("span", {
    className: "panel-link"
  }, "View all ", /*#__PURE__*/React.createElement(Icon, {
    name: "arrow-right",
    size: 13
  }))), /*#__PURE__*/React.createElement("div", {
    className: "rtable"
  }, /*#__PURE__*/React.createElement("div", {
    className: "rtable__head"
  }, /*#__PURE__*/React.createElement("span", null, "Run"), /*#__PURE__*/React.createElement("span", null, "Status"), /*#__PURE__*/React.createElement("span", null, "Test"), /*#__PURE__*/React.createElement("span", null, "Adapter"), /*#__PURE__*/React.createElement("span", {
    className: "num"
  }, "Steps"), /*#__PURE__*/React.createElement("span", null, "Provider"), /*#__PURE__*/React.createElement("span", {
    className: "num"
  }, "Duration"), /*#__PURE__*/React.createElement("span", {
    className: "num"
  }, "When")), runs.map(rn => /*#__PURE__*/React.createElement("div", {
    className: "rrow",
    key: rn.id
  }, /*#__PURE__*/React.createElement("span", {
    className: "rrow__id"
  }, rn.id), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement(Status, {
    status: rn.status
  })), /*#__PURE__*/React.createElement("span", {
    className: "rrow__name"
  }, rn.name), /*#__PURE__*/React.createElement("span", null, /*#__PURE__*/React.createElement(Tag, {
    dotColor: adapters[rn.adapter]
  }, rn.adapter)), /*#__PURE__*/React.createElement("span", {
    className: "num mono rrow__steps"
  }, rn.steps), /*#__PURE__*/React.createElement("span", {
    className: "mono rrow__prov"
  }, rn.provider), /*#__PURE__*/React.createElement("span", {
    className: "num mono"
  }, rn.dur), /*#__PURE__*/React.createElement("span", {
    className: "num rrow__when"
  }, rn.when)))));
}
function App() {
  useLucide();
  const {
    KPIS,
    TREND,
    FLAKY,
    RUNS,
    ADAPTERS
  } = window.DASH_DATA;
  const [tab, setTab] = useState("Overview");
  return /*#__PURE__*/React.createElement("div", {
    className: "dash"
  }, /*#__PURE__*/React.createElement(TopNav, {
    tab: tab,
    setTab: setTab
  }), /*#__PURE__*/React.createElement("main", {
    className: "dash__main"
  }, /*#__PURE__*/React.createElement("div", {
    className: "dash__head"
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("h1", null, "Overview"), /*#__PURE__*/React.createElement("p", {
    className: "dash__sub"
  }, "shop-frontend \xB7 all environments \xB7 auto-refresh 30s")), /*#__PURE__*/React.createElement("div", {
    className: "dash__head-actions"
  }, /*#__PURE__*/React.createElement("button", {
    className: "ds-btn ds-btn--secondary ds-btn--sm"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-btn__icon"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "calendar",
    size: 14
  })), "Last 7 days"), /*#__PURE__*/React.createElement("button", {
    className: "ds-btn ds-btn--secondary ds-btn--sm"
  }, /*#__PURE__*/React.createElement("span", {
    className: "ds-btn__icon"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "download",
    size: 14
  })), "Export"))), /*#__PURE__*/React.createElement("div", {
    className: "kpis"
  }, KPIS.map(k => /*#__PURE__*/React.createElement(KpiCard, {
    key: k.id,
    k: k
  }))), /*#__PURE__*/React.createElement("div", {
    className: "dash__grid"
  }, /*#__PURE__*/React.createElement(TrendChart, {
    trend: TREND
  }), /*#__PURE__*/React.createElement(FlakyTable, {
    flaky: FLAKY,
    adapters: ADAPTERS
  })), /*#__PURE__*/React.createElement(RunsTable, {
    runs: RUNS,
    adapters: ADAPTERS
  })));
}
ReactDOM.createRoot(document.getElementById("root")).render(/*#__PURE__*/React.createElement(App, null));
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/dashboard/app.jsx", error: String((e && e.message) || e) }); }

// ui_kits/dashboard/data.js
try { (() => {
/* Argus Dashboard — demo data */
(function () {
  const KPIS = [{
    id: "pass",
    label: "Pass rate",
    value: "94.2%",
    delta: "+1.8 pts",
    up: true,
    icon: "circle-check"
  }, {
    id: "runs",
    label: "Runs today",
    value: "247",
    delta: "+34",
    up: true,
    icon: "play"
  }, {
    id: "dur",
    label: "Avg duration",
    value: "7.4s",
    delta: "−0.6s",
    up: true,
    icon: "clock"
  }, {
    id: "flaky",
    label: "Flaky tests",
    value: "6",
    delta: "+2",
    up: false,
    icon: "zap"
  }];

  // 14-day trend (pass/fail counts per day)
  const TREND = [[210, 6], [198, 9], [221, 4], [205, 12], [230, 7], [241, 5], [219, 8], [233, 6], [247, 14], [238, 5], [251, 9], [244, 7], [239, 11], [247, 8]].map(([pass, fail], i) => ({
    day: i,
    pass,
    fail
  }));
  const r = s => s.split("").map(c => c === "p" ? "pass" : c === "f" ? "fail" : "skip");
  const FLAKY = [{
    name: "cart-persistence.test.yaml",
    adapter: "browser",
    score: 0.42,
    runs: r("ppfppfppppp".slice(0, 12))
  }, {
    name: "oauth-redirect.test.yaml",
    adapter: "browser",
    score: 0.31,
    runs: r("ppfppppfpppp")
  }, {
    name: "drag-reorder.test.yaml",
    adapter: "desktop-gui",
    score: 0.28,
    runs: r("pfpppppfpppp")
  }, {
    name: "stream-output.test.yaml",
    adapter: "tui",
    score: 0.22,
    runs: r("ppppfppppppp")
  }, {
    name: "file-watch.test.yaml",
    adapter: "electron",
    score: 0.18,
    runs: r("pppppppfpppp")
  }, {
    name: "ansi-colors.test.yaml",
    adapter: "cli",
    score: 0.11,
    runs: r("ppppppppfppp")
  }];
  const RUNS = [{
    id: "#4821",
    name: "checkout.test.yaml",
    adapter: "browser",
    status: "fail",
    dur: "10.7s",
    provider: "gemini-2.0-flash",
    when: "2m ago",
    steps: "6/9"
  }, {
    id: "#4820",
    name: "guest-checkout.test.yaml",
    adapter: "browser",
    status: "pass",
    dur: "8.4s",
    provider: "gpt-4o",
    when: "4m ago",
    steps: "7/7"
  }, {
    id: "#4819",
    name: "settings-dialog.test.yaml",
    adapter: "desktop-gui",
    status: "pass",
    dur: "5.2s",
    provider: "claude-3-5-sonnet",
    when: "6m ago",
    steps: "5/5"
  }, {
    id: "#4818",
    name: "migrate.test.yaml",
    adapter: "tui",
    status: "running",
    dur: "—",
    provider: "claude-3-5-sonnet",
    when: "now",
    steps: "2/6"
  }, {
    id: "#4817",
    name: "init-wizard.test.yaml",
    adapter: "cli",
    status: "pass",
    dur: "1.1s",
    provider: "llava:13b",
    when: "11m ago",
    steps: "4/4"
  }, {
    id: "#4816",
    name: "cart-persistence.test.yaml",
    adapter: "browser",
    status: "flaky",
    dur: "9.1s",
    provider: "claude-3-5-sonnet",
    when: "13m ago",
    steps: "6/7"
  }, {
    id: "#4815",
    name: "file-export.test.yaml",
    adapter: "electron",
    status: "pass",
    dur: "6.8s",
    provider: "gpt-4o",
    when: "15m ago",
    steps: "8/8"
  }, {
    id: "#4814",
    name: "error-toast.test.yaml",
    adapter: "browser",
    status: "error",
    dur: "3.0s",
    provider: "gpt-4o",
    when: "18m ago",
    steps: "2/5"
  }];
  const ADAPTERS = {
    browser: "var(--signal-500)",
    "desktop-gui": "var(--beacon-500)",
    cli: "var(--violet-500)",
    tui: "var(--blue-500)",
    electron: "var(--green-500)"
  };
  window.DASH_DATA = {
    KPIS,
    TREND,
    FLAKY,
    RUNS,
    ADAPTERS
  };
})();
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/dashboard/data.js", error: String((e && e.message) || e) }); }

__ds_ns.Button = __ds_scope.Button;

__ds_ns.IconButton = __ds_scope.IconButton;

__ds_ns.Badge = __ds_scope.Badge;

__ds_ns.StatusBadge = __ds_scope.StatusBadge;

__ds_ns.Tag = __ds_scope.Tag;

__ds_ns.ProgressBar = __ds_scope.ProgressBar;

__ds_ns.Tooltip = __ds_scope.Tooltip;

__ds_ns.Checkbox = __ds_scope.Checkbox;

__ds_ns.Input = __ds_scope.Input;

__ds_ns.Select = __ds_scope.Select;

__ds_ns.Switch = __ds_scope.Switch;

__ds_ns.Card = __ds_scope.Card;

__ds_ns.Tabs = __ds_scope.Tabs;

})();
