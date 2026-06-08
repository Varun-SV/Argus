/* Argus Dashboard — demo data */
(function () {
  const KPIS = [
    { id: "pass", label: "Pass rate", value: "94.2%", delta: "+1.8 pts", up: true, icon: "circle-check" },
    { id: "runs", label: "Runs today", value: "247", delta: "+34", up: true, icon: "play" },
    { id: "dur", label: "Avg duration", value: "7.4s", delta: "−0.6s", up: true, icon: "clock" },
    { id: "flaky", label: "Flaky tests", value: "6", delta: "+2", up: false, icon: "zap" },
  ];

  // 14-day trend (pass/fail counts per day)
  const TREND = [
    [210, 6], [198, 9], [221, 4], [205, 12], [230, 7], [241, 5], [219, 8],
    [233, 6], [247, 14], [238, 5], [251, 9], [244, 7], [239, 11], [247, 8],
  ].map(([pass, fail], i) => ({ day: i, pass, fail }));

  const r = (s) => s.split("").map((c) => (c === "p" ? "pass" : c === "f" ? "fail" : "skip"));
  const FLAKY = [
    { name: "cart-persistence.test.yaml", adapter: "browser", score: 0.42, runs: r("ppfppfppppp".slice(0, 12)) },
    { name: "oauth-redirect.test.yaml", adapter: "browser", score: 0.31, runs: r("ppfppppfpppp") },
    { name: "drag-reorder.test.yaml", adapter: "desktop-gui", score: 0.28, runs: r("pfpppppfpppp") },
    { name: "stream-output.test.yaml", adapter: "tui", score: 0.22, runs: r("ppppfppppppp") },
    { name: "file-watch.test.yaml", adapter: "electron", score: 0.18, runs: r("pppppppfpppp") },
    { name: "ansi-colors.test.yaml", adapter: "cli", score: 0.11, runs: r("ppppppppfppp") },
  ];

  const RUNS = [
    { id: "#4821", name: "checkout.test.yaml", adapter: "browser", status: "fail", dur: "10.7s", provider: "gemini-2.0-flash", when: "2m ago", steps: "6/9" },
    { id: "#4820", name: "guest-checkout.test.yaml", adapter: "browser", status: "pass", dur: "8.4s", provider: "gpt-4o", when: "4m ago", steps: "7/7" },
    { id: "#4819", name: "settings-dialog.test.yaml", adapter: "desktop-gui", status: "pass", dur: "5.2s", provider: "claude-3-5-sonnet", when: "6m ago", steps: "5/5" },
    { id: "#4818", name: "migrate.test.yaml", adapter: "tui", status: "running", dur: "—", provider: "claude-3-5-sonnet", when: "now", steps: "2/6" },
    { id: "#4817", name: "init-wizard.test.yaml", adapter: "cli", status: "pass", dur: "1.1s", provider: "llava:13b", when: "11m ago", steps: "4/4" },
    { id: "#4816", name: "cart-persistence.test.yaml", adapter: "browser", status: "flaky", dur: "9.1s", provider: "claude-3-5-sonnet", when: "13m ago", steps: "6/7" },
    { id: "#4815", name: "file-export.test.yaml", adapter: "electron", status: "pass", dur: "6.8s", provider: "gpt-4o", when: "15m ago", steps: "8/8" },
    { id: "#4814", name: "error-toast.test.yaml", adapter: "browser", status: "error", dur: "3.0s", provider: "gpt-4o", when: "18m ago", steps: "2/5" },
  ];

  const ADAPTERS = {
    browser: "var(--signal-500)", "desktop-gui": "var(--beacon-500)",
    cli: "var(--violet-500)", tui: "var(--blue-500)", electron: "var(--green-500)",
  };

  window.DASH_DATA = { KPIS, TREND, FLAKY, RUNS, ADAPTERS };
})();
