/* Argus CLI — streaming terminal recreation */

const GLYPH = { pass: "✓", fail: "✗", skip: "⊘", run: "●" };

function Line({ ln }) {
  switch (ln.type) {
    case "cmd":
      return <div className="tl"><span className="t-prompt">$</span> <span className="t-cmd">{hlCmd(ln.text)}</span></div>;
    case "banner":
      return <div className="tl t-banner">{ln.text}</div>;
    case "info":
      return <div className="tl"><span className="t-dim">→</span> <span className="t-dim">{ln.text}</span></div>;
    case "step":
    case "assert": {
      const isAssert = ln.type === "assert";
      return (
        <div className="tl tl-step">
          <span className={"t-g t-" + ln.status}>{GLYPH[ln.status]}</span>
          <span className="t-n">{isAssert ? "assert" : ln.n}</span>
          <span className="t-step-text">
            {isAssert ? <span className="t-assert">{ln.text}</span> : ln.text}
          </span>
          <span className="t-dur">{ln.dur}</span>
        </div>
      );
    }
    case "note":
      return <div className="tl tl-note"><span className="t-dim">↳ {ln.text}</span></div>;
    case "diff":
      return (
        <div className="tl tl-diff">
          <div><span className="t-dim">expected:</span> <span className="t-str">{ln.expected}</span></div>
          <div><span className="t-dim">actual:&nbsp;&nbsp;</span> <span className="t-fail">{ln.actual}</span></div>
        </div>
      );
    case "blank":
      return <div className="tl">&nbsp;</div>;
    case "rule":
      return <div className="tl t-rule">{"── " + (ln.text ? ln.text + " " : "")}{"".padEnd(ln.text ? 40 - ln.text.length : 46, "─")}</div>;
    case "summary":
      return (
        <div className="tl tl-summary">
          <span className="t-pass">{ln.pass} passed</span>
          <span className="t-dim"> · </span>
          <span className="t-fail">{ln.fail} failed</span>
          <span className="t-dim"> · </span>
          <span className="t-skip">{ln.skip} skipped</span>
          <span className="t-dim">   ·   {ln.dur}</span>
        </div>
      );
    case "report":
      return <div className="tl"><span className="t-dim">report →</span> <span className="t-link">{ln.text}</span></div>;
    case "exit":
      return <div className="tl"><span className="t-dim">exit code</span> <span className={ln.code === 0 ? "t-pass" : "t-fail"}>{ln.code}</span></div>;
    default:
      return null;
  }
}

function hlCmd(text) {
  const parts = text.split(" ");
  return parts.map((p, i) => {
    let cls = "t-arg";
    if (i === 0) cls = "t-bin";
    else if (i === 1) cls = "t-sub";
    else if (p.startsWith("--")) cls = "t-flag";
    else if (p.startsWith(".argus")) cls = "t-path";
    return <span key={i} className={cls}>{p}{i < parts.length - 1 ? " " : ""}</span>;
  });
}

function Terminal() {
  const { TRANSCRIPT } = window.CLI_DATA;
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
      if (i >= TRANSCRIPT.length) { clearInterval(timer.current); setPlaying(false); }
    }, 230);
  }

  useEffect(() => { play(); return () => clearInterval(timer.current); }, []);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [shown]);

  return (
    <div className="term">
      <div className="term__chrome">
        <span className="term__dots"><i /><i /><i /></span>
        <span className="term__title">argus — zsh — 96×30</span>
        <button className="term__replay" onClick={play} disabled={playing}>
          <Icon name={playing ? "loader" : "rotate-ccw"} size={13} /> {playing ? "running" : "replay"}
        </button>
      </div>
      <div className="term__body" ref={bodyRef}>
        {TRANSCRIPT.slice(0, shown).map((ln, i) => <Line key={i} ln={ln} />)}
        {playing && <div className="tl"><span className="term__cursor" /></div>}
        {!playing && <div className="tl"><span className="t-prompt">$</span> <span className="term__cursor" /></div>}
      </div>
    </div>
  );
}

function CommandRail() {
  const cmds = [
    { c: "argus init", d: "scaffold .argus/ + first test" },
    { c: "argus run", d: "run a test or suite" },
    { c: "argus watch", d: "re-run on file change" },
    { c: "argus record", d: "capture steps interactively" },
    { c: "argus report", d: "open the HTML report" },
    { c: "argus serve", d: "launch the web dashboard" },
  ];
  return (
    <aside className="rail">
      <div className="rail__head"><img src="../../assets/logo-mark.svg" width="20" height="20" alt="" /><span>argus</span><span className="rail__v">v0.4.1</span></div>
      <p className="rail__lead">One binary for developers and CI. Exit codes follow test-runner convention: <span className="mono">0</span> pass, <span className="mono">1</span> fail, <span className="mono">2</span> error.</p>
      <div className="rail__cmds">
        {cmds.map((x) => (
          <div className="rail__cmd" key={x.c}>
            <code>{x.c}</code>
            <span>{x.d}</span>
          </div>
        ))}
      </div>
      <div className="rail__env">
        <span className="rail__env-title">ENV</span>
        <code>ARGUS_PROVIDER=anthropic</code>
        <code>ARGUS_MODEL=claude-3-5-sonnet</code>
        <code>ARGUS_ADAPTER=browser</code>
      </div>
    </aside>
  );
}

function App() {
  useLucide();
  return (
    <div className="cli">
      <CommandRail />
      <main className="cli__main"><Terminal /></main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
