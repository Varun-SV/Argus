/* Argus Console — app shell, titlebar, status bar, view switching */

function TitleBar({ view, setView, running, onRunAll, watch, setWatch }) {
  return (
    <header className="titlebar">
      <div className="titlebar__left">
        <span className="win-dots"><i /><i /><i /></span>
        <span className="brand">
          <img src="../../assets/logo-mark.svg" width="18" height="18" alt="" />
          <span className="brand__name">Argus</span>
        </span>
        <span className="proj">
          <Icon name="git-branch" size={13} />
          shop-frontend
          <Icon name="chevron-down" size={13} />
        </span>
      </div>

      <nav className="viewnav">
        {[
          { id: "runs", label: "Runs", icon: "layout-list" },
          { id: "editor", label: "Editor", icon: "file-code-2" },
          { id: "providers", label: "Providers", icon: "sparkles" },
        ].map((v) => (
          <button key={v.id} className={"viewnav__tab" + (view === v.id ? " is-active" : "")} onClick={() => setView(v.id)}>
            <Icon name={v.icon} size={15} />
            {v.label}
          </button>
        ))}
      </nav>

      <div className="titlebar__right">
        <button className={"watch" + (watch ? " is-on" : "")} onClick={() => setWatch(!watch)} title="Watch mode">
          <Icon name="eye" size={14} />
          watch
        </button>
        <Tag icon={<Icon name="sparkles" size={12} />}>claude-3-5-sonnet</Tag>
        <Btn variant="primary" size="sm" leftIcon={<Icon name={running ? "loader" : "play"} size={14} />} onClick={onRunAll}>
          {running ? "Running" : "Run all"}
        </Btn>
      </div>
    </header>
  );
}

function StatusBar({ test, running }) {
  return (
    <footer className="statusbar">
      <div className="statusbar__group">
        <span className="statusbar__item"><Icon name="git-branch" size={12} /> main</span>
        <span className="statusbar__item statusbar__item--ok"><span className="sdot sdot--pass" /> connected</span>
        <span className="statusbar__item"><Icon name="cpu" size={12} /> ollama · localhost:11434</span>
      </div>
      <div className="statusbar__group">
        <span className="statusbar__item">{running ? "running checkout.test.yaml…" : `${test.passed}/${test.total} steps`}</span>
        <span className={"statusbar__item " + (test.status === "fail" ? "statusbar__item--fail" : "statusbar__item--ok")}>
          exit {test.status === "pass" ? 0 : test.status === "fail" ? 1 : "—"}
        </span>
        <span className="statusbar__item statusbar__item--muted">argus v0.4.1</span>
      </div>
    </footer>
  );
}

function App() {
  useLucide();
  const { SUITES } = window.ARGUS_DATA;
  const allTests = SUITES.flatMap((s) => s.tests);
  const [view, setView] = useState("runs");
  const [activeTestId, setActiveTestId] = useState("t1");
  const [selectedStepId, setSelectedStepId] = useState("s6");
  const [query, setQuery] = useState("");
  const [running, setRunning] = useState(false);
  const [watch, setWatch] = useState(true);

  const test = allTests.find((t) => t.id === activeTestId) || allTests[0];
  const steps = test.steps || window.ARGUS_DATA.checkoutSteps;

  function runOnce() {
    setRunning(true);
    setTimeout(() => setRunning(false), 1800);
  }

  function selectTest(id) {
    setActiveTestId(id);
    setView("runs");
    const t = allTests.find((x) => x.id === id);
    if (t && t.steps) setSelectedStepId(t.steps.find((s) => s.status === "fail")?.id || t.steps[0].id);
  }

  return (
    <div className="ds-app">
      <TitleBar view={view} setView={setView} running={running} onRunAll={runOnce} watch={watch} setWatch={setWatch} />
      <div className="workbench">
        <Sidebar suites={SUITES} activeTestId={activeTestId} onSelectTest={selectTest} query={query} setQuery={setQuery} />
        <main className="main">
          {view === "runs" && (
            <RunDetail
              test={test}
              steps={steps}
              selectedStepId={selectedStepId}
              onSelectStep={setSelectedStepId}
              running={running}
              onRun={runOnce}
            />
          )}
          {view === "editor" && <Editor />}
          {view === "providers" && <Providers />}
        </main>
        {view === "runs" && <Inspector step={steps.find((s) => s.id === selectedStepId) || steps[0]} />}
      </div>
      <StatusBar test={test} running={running} />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
