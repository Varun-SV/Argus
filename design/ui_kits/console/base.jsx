/* Argus Console — shared atoms (Lucide icon + thin wrappers over the DS .ds-* classes) */

const { useState, useEffect, useRef } = React;

/* Lucide icon. Renders a placeholder <i> that lucide.createIcons() swaps for an <svg>.
   Size is controlled by font-size (CSS rule `.lucide{width:1em;height:1em}`). */
function Icon({ name, size = 16, sw = 2, className = "", style = {} }) {
  return (
    <i
      data-lucide={name}
      data-sw={sw}
      className={"ic " + className}
      style={{ fontSize: size, width: size, height: size, ...style }}
    />
  );
}

/* Call after every render so freshly-mounted <i data-lucide> get replaced. */
function useLucide() {
  useEffect(() => {
    if (window.lucide) {
      window.lucide.createIcons({ attrs: { "stroke-width": 1.9 } });
    }
  });
}

const STATUS_LABEL = {
  pass: "Passed", fail: "Failed", error: "Error",
  running: "Running", skipped: "Skipped", flaky: "Flaky", pending: "Pending",
};

function Status({ status, label, solid }) {
  const cls = ["ds-status", `ds-status--${status === "pending" ? "skipped" : status}`, solid && "ds-status--solid"]
    .filter(Boolean).join(" ");
  return (
    <span className={cls}>
      <span className="ds-status__dot" />
      {label || STATUS_LABEL[status] || status}
    </span>
  );
}

function Tag({ children, dotColor, icon, onClick, className = "" }) {
  return (
    <span className={"ds-tag " + (onClick ? "ds-tag--interactive " : "") + className} onClick={onClick}>
      {dotColor && <span className="ds-tag__dot" style={{ background: dotColor }} />}
      {icon}
      {children}
    </span>
  );
}

function Btn({ variant = "primary", size, leftIcon, children, className = "", ...rest }) {
  const cls = ["ds-btn", `ds-btn--${variant}`, size && `ds-btn--${size}`, className].filter(Boolean).join(" ");
  return (
    <button className={cls} {...rest}>
      {leftIcon && <span className="ds-btn__icon">{leftIcon}</span>}
      {children}
    </button>
  );
}

function IconBtn({ name, label, active, variant, size, onClick }) {
  const cls = ["ds-iconbtn", variant === "solid" && "ds-iconbtn--solid", size === "sm" && "ds-iconbtn--sm",
    active && "ds-iconbtn--active"].filter(Boolean).join(" ");
  return (
    <button className={cls} aria-label={label} title={label} onClick={onClick}>
      <Icon name={name} size={size === "sm" ? 15 : 17} />
    </button>
  );
}

/* A small "captured frame" — abstract wireframe of the app under test with an
   annotation overlay (what Argus is looking at). No real product UI is fabricated. */
function CapturedFrame({ status = "pass", label = "Order summary", caption, big }) {
  const ring =
    status === "fail" ? "var(--status-fail)" :
    status === "running" ? "var(--status-running)" : "var(--status-pass)";
  return (
    <div className={"frame" + (big ? " frame--big" : "")}>
      <div className="frame__chrome">
        <span className="frame__dot" /><span className="frame__dot" /><span className="frame__dot" />
        <span className="frame__url">shop.example.com</span>
      </div>
      <div className="frame__body">
        <div className="wf wf--bar" />
        <div className="wf-row">
          <div className="wf wf--side" />
          <div className="wf-main">
            <div className="wf wf--h" />
            <div className="wf wf--p" />
            <div className="wf wf--p short" />
            <div className="frame__target" style={{ borderColor: ring, boxShadow: `0 0 0 3px ${ring}22` }}>
              <span className="frame__target-label" style={{ background: ring }}>{label}</span>
              <div className="wf wf--btn" />
            </div>
          </div>
        </div>
      </div>
      {caption && <div className="frame__caption">{caption}</div>}
      <span className="frame__reticle frame__reticle--tl" />
      <span className="frame__reticle frame__reticle--br" />
    </div>
  );
}

Object.assign(window, { Icon, useLucide, Status, Tag, Btn, IconBtn, CapturedFrame, STATUS_LABEL });
