/* Argus Console — right inspector: selected step detail */

function Inspector({ step }) {
  if (!step) return <aside className="inspector" />;
  const isFail = step.status === "fail";
  return (
    <aside className="inspector">
      <div className="inspector__head">
        <span className="inspector__kind">{step.kind}</span>
        <Status status={step.status} />
      </div>

      <div className="inspector__title">
        {step.kind === "assert" ? <code>{step.text}</code> : step.text}
      </div>

      <div className="inspector__scroll">
        {step.action && (
          <div className="insp-block">
            <div className="insp-block__label"><Icon name="mouse-pointer-click" size={13} /> What Argus did</div>
            <p className="insp-block__body">{step.action}</p>
          </div>
        )}

        {step.reasoning && (
          <div className="insp-block">
            <div className="insp-block__label"><Icon name="sparkles" size={13} /> Model reasoning</div>
            <p className="insp-block__body insp-block__body--quote">{step.reasoning}</p>
          </div>
        )}

        {step.kind === "assert" && (
          <div className="insp-block">
            <div className="insp-block__label"><Icon name="scan-line" size={13} /> Assertion</div>
            <div className="diff">
              <div className="diff__row diff__row--exp">
                <span className="diff__tag">expected</span>
                <code>{step.expected}</code>
              </div>
              <div className={"diff__row " + (isFail ? "diff__row--act" : "diff__row--ok")}>
                <span className="diff__tag">{isFail ? "actual" : "matched"}</span>
                <code>{isFail ? step.actual : step.expected}</code>
              </div>
            </div>
          </div>
        )}

        <div className="insp-block">
          <div className="insp-block__label"><Icon name="terminal" size={13} /> Log</div>
          <pre className="insp-log">
{`[obs] capture screenshot → frame ${step.frame}
[obs] a11y tree · 142 nodes
[act] ${step.kind} · ${step.dur}`}
{isFail ? `\n[assert] FAILED — expected substring not found\n[exit] 1` : `\n[assert] ok\n[exit] 0`}
          </pre>
        </div>
      </div>

      <div className="inspector__foot">
        <Btn variant="ghost" size="sm" leftIcon={<Icon name="copy" size={13} />}>Copy</Btn>
        <Btn variant="secondary" size="sm" leftIcon={<Icon name="image" size={13} />}>Open frame</Btn>
      </div>
    </aside>
  );
}

window.Inspector = Inspector;
