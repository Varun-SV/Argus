/* Argus Console — Editor view: YAML source + live step preview */

function hl(line, i) {
  const t = line.replace(/\t/g, "  ");
  // comment
  if (/^\s*#/.test(t)) return <span className="y-c">{t}</span>;
  // key: value  (optionally with leading "- ")
  const m = t.match(/^(\s*)(-\s+)?([A-Za-z0-9_]+)(:)(.*)$/);
  if (m) {
    const [, indent, dash, key, colon, rest] = m;
    return (
      <span>
        {indent}{dash && <span className="y-p">{dash}</span>}
        <span className="y-k">{key}</span><span className="y-p">{colon}</span>
        {hlValue(rest)}
      </span>
    );
  }
  // list item: - "string"
  const l = t.match(/^(\s*)(-\s+)(.*)$/);
  if (l) {
    const [, indent, dash, rest] = l;
    return <span>{indent}<span className="y-p">{dash}</span>{hlValue(" " + rest).props ? hlValue(rest) : rest}</span>;
  }
  return <span>{t}</span>;
}

function hlValue(rest) {
  if (!rest.trim()) return rest;
  const s = rest.match(/^(\s*)("[^"]*"|[^#]*)(\s*#.*)?$/);
  if (!s) return rest;
  const [, sp, val, cmt] = s;
  const isStr = /^".*"$/.test(val.trim());
  const isNum = /^-?\d+(\.\d+)?$/.test(val.trim());
  return (
    <span>
      {sp}
      <span className={isStr ? "y-s" : isNum ? "y-n" : "y-v"}>{val}</span>
      {cmt && <span className="y-c">{cmt}</span>}
    </span>
  );
}

function Editor() {
  const { YAML, checkoutSteps } = window.ARGUS_DATA;
  const lines = YAML.replace(/\n$/, "").split("\n");
  return (
    <section className="editor">
      <div className="editor__pane editor__pane--code">
        <div className="pane-head">
          <Icon name="file-code-2" size={15} />
          <span>.argus/checkout.test.yaml</span>
          <span className="pane-head__spacer" />
          <Tag dotColor="var(--signal-500)">browser</Tag>
          <IconBtn name="wrap-text" label="Wrap" size="sm" />
        </div>
        <div className="code">
          <pre className="code__gutter">{lines.map((_, i) => <div key={i}>{i + 1}</div>)}</pre>
          <pre className="code__body">{lines.map((ln, i) => <div className="code__line" key={i}>{hl(ln, i)}</div>)}</pre>
        </div>
      </div>

      <div className="editor__pane editor__pane--preview">
        <div className="pane-head">
          <Icon name="list-checks" size={15} />
          <span>Resolved steps</span>
          <span className="pane-head__spacer" />
          <span className="pane-head__hint">hybrid-agentic</span>
        </div>
        <div className="preview">
          <div className="preview__target">
            <div className="preview__target-row"><span className="k">adapter</span><Tag dotColor="var(--signal-500)">browser</Tag></div>
            <div className="preview__target-row"><span className="k">launch</span><code className="mono">https://shop.example.com</code></div>
            <div className="preview__target-row"><span className="k">provider</span><Tag icon={<Icon name="sparkles" size={12} />}>claude-3-5-sonnet</Tag></div>
          </div>
          <ol className="preview__steps">
            {checkoutSteps.filter((s) => s.kind === "step" || s.kind === "assert").map((s, i) => (
              <li key={s.id} className={"pstep pstep--" + s.kind}>
                <span className="pstep__n">{i + 1}</span>
                <span className="pstep__icon"><Icon name={s.kind === "assert" ? "scan-line" : "mouse-pointer-click"} size={13} /></span>
                <span className="pstep__text">
                  {s.kind === "assert" ? <code>{s.text}</code> : s.text}
                </span>
                {s.kind === "assert" && <span className="pstep__badge">assert</span>}
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}

window.Editor = Editor;
