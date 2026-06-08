/* Argus Console — Providers view: configure the provider-agnostic layer */

function ProviderCard({ p, active, onActivate }) {
  const connected = p.status === "connected";
  return (
    <div className={"pcard" + (active ? " is-active" : "")} onClick={() => onActivate(p.id)}>
      <div className="pcard__top">
        <span className="pcard__logo"><Icon name="sparkles" size={16} /></span>
        <div className="pcard__id">
          <span className="pcard__name">{p.name}</span>
          <code className="pcard__model">{p.model}</code>
        </div>
        <span className={"pcard__status pcard__status--" + (connected ? "on" : "off")}>
          <span className="sdot" style={{ background: connected ? "var(--status-pass)" : "var(--text-faint)" }} />
          {connected ? "Connected" : "Idle"}
        </span>
      </div>
      <div className="pcard__note">{p.note}</div>
      {active && (
        <div className="pcard__primary"><Icon name="check" size={12} /> Active provider</div>
      )}
    </div>
  );
}

function Providers() {
  const { PROVIDERS } = window.ARGUS_DATA;
  const [active, setActive] = useState("anthropic");
  return (
    <section className="providers">
      <div className="providers__head">
        <div>
          <h2>Providers</h2>
          <p className="providers__sub">Argus is provider-agnostic. Pick a default; override per-project or per-test.</p>
        </div>
        <Btn variant="secondary" size="sm" leftIcon={<Icon name="plus" size={14} />}>Add provider</Btn>
      </div>

      <div className="providers__grid">
        {PROVIDERS.map((p) => (
          <ProviderCard key={p.id} p={p} active={p.id === active} onActivate={setActive} />
        ))}
      </div>

      <div className="providers__config">
        <div className="pane-head">
          <Icon name="sliders-horizontal" size={15} />
          <span>Connection · {PROVIDERS.find((p) => p.id === active).name}</span>
        </div>
        <div className="config-grid">
          <label className="ds-field"><span className="ds-label">Model</span>
            <div className="ds-select-wrap">
              <select className="ds-select" defaultValue={PROVIDERS.find((p) => p.id === active).model}>
                <option>{PROVIDERS.find((p) => p.id === active).model}</option>
                <option>claude-3-opus</option>
              </select>
            </div>
          </label>
          <label className="ds-field"><span className="ds-label">API base URL</span>
            <input className="ds-input ds-input--mono" defaultValue="https://api.anthropic.com" />
          </label>
          <label className="ds-field"><span className="ds-label">API key <span className="ds-label__req">*</span></span>
            <input className="ds-input ds-input--mono" type="password" defaultValue="sk-ant-api03-••••••••" />
          </label>
          <label className="ds-field"><span className="ds-label">Vision detail</span>
            <div className="ds-select-wrap">
              <select className="ds-select" defaultValue="auto"><option>auto</option><option>high</option><option>low</option></select>
            </div>
          </label>
        </div>
        <div className="config-foot">
          <label className="ds-switch"><input type="checkbox" defaultChecked /><span className="ds-switch__track"><span className="ds-switch__thumb" /></span><span className="ds-switch__label">Use for all projects</span></label>
          <div className="config-foot__actions">
            <Btn variant="ghost" size="sm">Test connection</Btn>
            <Btn variant="primary" size="sm" leftIcon={<Icon name="check" size={14} />}>Save</Btn>
          </div>
        </div>
      </div>
    </section>
  );
}

window.Providers = Providers;
