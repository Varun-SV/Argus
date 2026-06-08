/* Argus Dashboard — self-hosted web dashboard (single screen) */

function TopNav({ tab, setTab }) {
  return (
    <header className="dnav">
      <div className="dnav__brand">
        <img src="../../assets/logo-mark.svg" width="22" height="22" alt="" />
        <span className="dnav__name">Argus</span>
        <span className="dnav__sub">dashboard</span>
      </div>
      <nav className="dnav__tabs">
        {["Overview", "Runs", "Tests", "Flakiness"].map((t) => (
          <button key={t} className={"dnav__tab" + (tab === t ? " is-active" : "")} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>
      <div className="dnav__right">
        <span className="dnav__search"><Icon name="search" size={14} /> Search runs…</span>
        <span className="dnav__env"><span className="sdot sdot--pass" /> staging <Icon name="chevron-down" size={13} /></span>
        <span className="dnav__avatar">QA</span>
      </div>
    </header>
  );
}

function KpiCard({ k }) {
  return (
    <div className="kpi">
      <div className="kpi__top">
        <span className="kpi__icon"><Icon name={k.icon} size={15} /></span>
        <span className="kpi__label">{k.label}</span>
      </div>
      <div className="kpi__value">{k.value}</div>
      <div className={"kpi__delta " + (k.up ? "kpi__delta--up" : "kpi__delta--down")}>
        <Icon name={k.up ? "trending-up" : "trending-down"} size={13} /> {k.delta}
        <span className="kpi__delta-note">vs last week</span>
      </div>
    </div>
  );
}

function TrendChart({ trend }) {
  const max = Math.max(...trend.map((d) => d.pass + d.fail));
  return (
    <div className="card-panel trend">
      <div className="panel-head">
        <div><span className="panel-title">Pass / fail trend</span><span className="panel-sub">last 14 days</span></div>
        <div className="trend__legend">
          <span><span className="lg lg--pass" /> pass</span>
          <span><span className="lg lg--fail" /> fail</span>
        </div>
      </div>
      <div className="trend__plot">
        {[100, 75, 50, 25, 0].map((g) => <div className="trend__grid" key={g} style={{ bottom: g + "%" }} />)}
        <div className="trend__bars">
          {trend.map((d, i) => {
            const total = d.pass + d.fail;
            return (
              <div className="trend__col" key={i} title={`${d.pass} pass · ${d.fail} fail`}>
                <div className="trend__stack" style={{ height: (total / max) * 100 + "%" }}>
                  <div className="trend__fail" style={{ height: (d.fail / total) * 100 + "%" }} />
                  <div className="trend__pass" style={{ height: (d.pass / total) * 100 + "%" }} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Sparkline({ runs }) {
  return (
    <span className="spark">
      {runs.map((s, i) => <span key={i} className={"spark__c spark__c--" + s} />)}
    </span>
  );
}

function FlakyTable({ flaky, adapters }) {
  return (
    <div className="card-panel">
      <div className="panel-head">
        <div><span className="panel-title">Flakiness</span><span className="panel-sub">tests with non-deterministic outcomes</span></div>
        <span className="panel-link">View all <Icon name="arrow-right" size={13} /></span>
      </div>
      <div className="ftable">
        {flaky.map((f) => (
          <div className="frow" key={f.name}>
            <span className="frow__dot" style={{ background: adapters[f.adapter] }} />
            <span className="frow__name">{f.name}</span>
            <Sparkline runs={f.runs} />
            <span className="frow__score">
              <span className="frow__bar"><span style={{ width: f.score * 100 + "%" }} /></span>
              <span className="frow__pct">{Math.round(f.score * 100)}%</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RunsTable({ runs, adapters }) {
  return (
    <div className="card-panel">
      <div className="panel-head">
        <div><span className="panel-title">Recent runs</span><span className="panel-sub">across all adapters</span></div>
        <span className="panel-link">View all <Icon name="arrow-right" size={13} /></span>
      </div>
      <div className="rtable">
        <div className="rtable__head">
          <span>Run</span><span>Status</span><span>Test</span><span>Adapter</span><span className="num">Steps</span><span>Provider</span><span className="num">Duration</span><span className="num">When</span>
        </div>
        {runs.map((rn) => (
          <div className="rrow" key={rn.id}>
            <span className="rrow__id">{rn.id}</span>
            <span><Status status={rn.status} /></span>
            <span className="rrow__name">{rn.name}</span>
            <span><Tag dotColor={adapters[rn.adapter]}>{rn.adapter}</Tag></span>
            <span className="num mono rrow__steps">{rn.steps}</span>
            <span className="mono rrow__prov">{rn.provider}</span>
            <span className="num mono">{rn.dur}</span>
            <span className="num rrow__when">{rn.when}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function App() {
  useLucide();
  const { KPIS, TREND, FLAKY, RUNS, ADAPTERS } = window.DASH_DATA;
  const [tab, setTab] = useState("Overview");
  return (
    <div className="dash">
      <TopNav tab={tab} setTab={setTab} />
      <main className="dash__main">
        <div className="dash__head">
          <div>
            <h1>Overview</h1>
            <p className="dash__sub">shop-frontend · all environments · auto-refresh 30s</p>
          </div>
          <div className="dash__head-actions">
            <button className="ds-btn ds-btn--secondary ds-btn--sm"><span className="ds-btn__icon"><Icon name="calendar" size={14} /></span>Last 7 days</button>
            <button className="ds-btn ds-btn--secondary ds-btn--sm"><span className="ds-btn__icon"><Icon name="download" size={14} /></span>Export</button>
          </div>
        </div>

        <div className="kpis">{KPIS.map((k) => <KpiCard key={k.id} k={k} />)}</div>

        <div className="dash__grid">
          <TrendChart trend={TREND} />
          <FlakyTable flaky={FLAKY} adapters={ADAPTERS} />
        </div>

        <RunsTable runs={RUNS} adapters={ADAPTERS} />
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
