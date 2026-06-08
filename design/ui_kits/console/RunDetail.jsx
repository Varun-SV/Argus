/* Argus Console — center stage for the Runs view */

function RunHeader({ test, running, onRun }) {
  const { ADAPTERS } = window.ARGUS_DATA;
  const ad = ADAPTERS[test.adapter];
  const passed = test.passed, total = test.total;
  const failed = total - passed - (test.status === "running" ? total - passed : 0);
  const segs = [
    { kind: "pass", value: passed },
    { kind: "fail", value: test.status === "fail" ? 1 : 0 },
    { kind: "skip", value: Math.max(0, total - passed - (test.status === "fail" ? 1 : 0)) },
  ];
  return (
    <div className="run-head">
      <div className="run-head__top">
        <div className="run-head__title">
          <Icon name="file-code-2" size={18} />
          <h2>{test.name}</h2>
          {running ? <Status status="running" /> : <Status status={test.status} />}
        </div>
        <div className="run-head__actions">
          <IconBtn name="history" label="Run history" />
          <IconBtn name="more-horizontal" label="More" />
          <Btn variant={running ? "secondary" : "primary"} size="sm"
               leftIcon={<Icon name={running ? "loader" : "play"} size={14} />} onClick={onRun}>
            {running ? "Running…" : "Re-run"}
          </Btn>
        </div>
      </div>
      <div className="run-head__meta">
        <Tag dotColor={ad.color}>{ad.label}</Tag>
        <Tag icon={<Icon name="sparkles" size={12} />}>{test.provider}</Tag>
        <span className="run-head__stat"><Icon name="check" size={13} /> {passed}/{total} steps</span>
        <span className="run-head__stat"><Icon name="clock" size={13} /> {test.dur}</span>
        <span className="run-head__stat run-head__stat--muted">exit {test.status === "pass" ? "0" : test.status === "fail" ? "1" : "—"}</span>
      </div>
      <div className="run-head__bar">
        <div className="ds-progress">
          {segs.map((s, i) =>
            s.value > 0 ? (
              <div key={i} className={"ds-progress__seg ds-progress__seg--" + s.kind}
                   style={{ width: (s.value / total) * 100 + "%" }} />
            ) : null
          )}
        </div>
      </div>
    </div>
  );
}

const KIND_GLYPH = { setup: "settings-2", teardown: "power", assert: "scan-line", step: "mouse-pointer-click" };

function StepRow({ step, n, active, onClick }) {
  return (
    <button className={"step" + (active ? " is-active" : "") + (" step--" + step.status)} onClick={onClick}>
      <span className="step__n">{String(n).padStart(2, "0")}</span>
      <span className={"sdot sdot--" + step.status} />
      <span className="step__icon"><Icon name={KIND_GLYPH[step.kind]} size={14} /></span>
      <span className="step__text">
        {step.kind === "assert" ? <code className="step__assert">{step.text}</code> : step.text}
        {step.kind !== "step" && step.kind !== "assert" && (
          <span className="step__kind">{step.kind}</span>
        )}
      </span>
      <span className="step__dur">{step.dur}</span>
    </button>
  );
}

function RunDetail({ test, steps, selectedStepId, onSelectStep, running, onRun }) {
  const sel = steps.find((s) => s.id === selectedStepId) || steps[0];
  return (
    <section className="run">
      <RunHeader test={test} running={running} onRun={onRun} />

      <div className="run__stage">
        <CapturedFrame
          big
          status={sel.status === "fail" ? "fail" : "pass"}
          label={sel.kind === "assert" ? sel.expected : "target"}
          caption={`frame ${sel.frame} · 1280×800 · ${sel.kind}`}
        />
        <div className="filmstrip">
          {steps.map((s, i) => (
            <button
              key={s.id}
              className={"filmstrip__item" + (s.id === sel.id ? " is-active" : "")}
              onClick={() => onSelectStep(s.id)}
              title={s.text}
            >
              <span className="filmstrip__thumb">
                <span className={"filmstrip__badge sdot--" + s.status} />
                <span className="filmstrip__frame">{s.frame}</span>
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="run__steps">
        <div className="run__steps-head">
          <span>Step timeline</span>
          <span className="run__steps-count">{steps.length} steps</span>
        </div>
        {steps.map((s, i) => (
          <StepRow key={s.id} step={s} n={i} active={s.id === sel.id} onClick={() => onSelectStep(s.id)} />
        ))}
      </div>
    </section>
  );
}

window.RunDetail = RunDetail;
