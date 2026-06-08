/* Argus Console — left sidebar: project + test tree */

function Sidebar({ suites, activeTestId, onSelectTest, query, setQuery }) {
  const { ADAPTERS } = window.ARGUS_DATA;
  return (
    <aside className="sidebar">
      <div className="sidebar__search">
        <Icon name="search" size={15} />
        <input
          className="sidebar__input"
          placeholder="Search tests…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <kbd className="sidebar__kbd">⌘K</kbd>
      </div>

      <div className="sidebar__scroll">
        <div className="tree">
          {suites.map((suite) => {
            const tests = suite.tests.filter((t) =>
              t.name.toLowerCase().includes(query.toLowerCase())
            );
            if (!tests.length) return null;
            return (
              <div className="tree__group" key={suite.name}>
                <div className="tree__suite">
                  <Icon name="chevron-down" size={13} />
                  <Icon name="folder" size={14} />
                  <span>{suite.name}</span>
                  <span className="tree__count">{suite.tests.length}</span>
                </div>
                {tests.map((t) => (
                  <button
                    key={t.id}
                    className={"tree__test" + (t.id === activeTestId ? " is-active" : "")}
                    onClick={() => onSelectTest(t.id)}
                  >
                    <span className={"sdot sdot--" + t.status} />
                    <span className="tree__name">{t.name}</span>
                    {t.flaky && <Icon name="zap" size={12} className="tree__flaky" />}
                    <span className="tree__dur">{t.dur}</span>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </div>

      <div className="sidebar__legend">
        <span className="legend__title">Adapters</span>
        <div className="legend__chips">
          {Object.entries(ADAPTERS).map(([k, v]) => (
            <span className="legend__chip" key={k}>
              <span className="legend__dot" style={{ background: v.color }} />
              {v.label}
            </span>
          ))}
        </div>
      </div>
    </aside>
  );
}

window.Sidebar = Sidebar;
