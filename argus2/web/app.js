"use strict";
/* Argus UI. Plain DOM, no framework: every view is a function returning a node. */

const S = {
  state: null, view: "home", tests: [], results: [], browser: null,
  home: { type: "web", url: "", command: "", behaviour: "" },
  explore: { type: "web", url: "", command: "", goal: "", budget: 25 },
  edit: null,          // {file, content, check}
  result: null,        // result being viewed
  shot: null,          // {id, file} picked in a result
  live: null,          // run being watched
};

// ---------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "open") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
async function api(method, path, body) {
  let res;
  try {
    res = await fetch("/api/" + path, {
      method, headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Error("Argus is not running any more. Start it again.");
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty */ }
  if (!res.ok) { const err = new Error(data.error || "Something went wrong (" + res.status + ")."); err.status = res.status; throw err; }
  return data;
}
let toastTimer;
function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text; el.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}
async function attempt(fn) { try { return await fn(); } catch (e) { toast(e.message); return undefined; } }
const when = t => t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
const secs = d => d == null ? "" : d < 60 ? d.toFixed(1) + "s" : Math.floor(d / 60) + "m " + Math.round(d % 60) + "s";
const WORD = { passed: "Passed", failed: "Failed", error: "Could not run", stopped: "Stopped", completed: "Finished", running: "Running", skipped: "Skipped", pending: "Waiting" };
const MARK = { passed: "✓", failed: "✕", error: "!", stopped: "■", skipped: "–", pending: "", running: "…" };
const badge = (status, text) => h("span", { class: "badge " + status }, text || WORD[status] || status);
const targetText = t => !t ? "" : t.type === "web" ? t.url : (t.command || "command line");
function modal(title, body, buttons) {
  const root = document.getElementById("modal-root");
  const close = () => root.replaceChildren();
  const box = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
    h("header", {}, title), h("div", { class: "body" }, body),
    h("footer", {}, (buttons || []).map(b => h("button", { class: "btn " + (b.kind || ""), onclick: () => b.run(close) }, b.label)),
      h("button", { class: "btn", onclick: close }, buttons && buttons.length ? "Cancel" : "Close")));
  const back = h("div", { class: "modal-back", onclick: e => { if (e.target === back) close(); } }, box);
  root.replaceChildren(back);
  const first = box.querySelector("input,textarea,button"); if (first) first.focus();
  return close;
}
function go(view) { S.view = view; render(); document.getElementById("main").scrollTop = 0; }

// ---------------------------------------------------------------- data
async function refresh() {
  S.state = await api("GET", "state");
  [S.tests, S.results] = await Promise.all([api("GET", "tests"), api("GET", "results")]);
}
async function loadBrowser() { S.browser = await attempt(() => api("GET", "browser")); renderChrome(); }
const modelMissing = () => S.state.settings.model_missing;

// ---------------------------------------------------------------- chrome
const NAV = [["home", "Test something"], ["tests", "Saved tests"], ["explore", "Explore"], ["results", "Results"], ["settings", "Settings"]];
function renderChrome() {
  const nav = document.getElementById("nav");
  const items = NAV.map(([id, label]) => h("button", {
    "aria-current": (S.view === id || (id === "tests" && S.view === "edit") || (id === "results" && S.view === "result")) ? "page" : null,
    onclick: () => go(id) }, label, id === "tests" && S.tests.length ? h("span", { class: "muted small", style: "margin-left:auto" }, S.tests.length) : null));
  if (S.live) items.splice(1, 0, h("button", { "aria-current": S.view === "live" ? "page" : null, onclick: () => go("live") },
    S.live.running ? "Running now" : "Last run", S.live.running ? h("span", { class: "dot" }) : null));
  nav.replaceChildren(...items);
  document.getElementById("ws-path").textContent = S.state.workspace;
  const st = S.state.settings;
  document.getElementById("ready").replaceChildren(...[
    h("span", {}, h("i", { class: st.model_missing ? "bad" : "ok" }),
      st.model_missing ? h("button", { onclick: () => go("settings") }, "Set up a model") : h("span", { title: st.provider + " · " + st.model, style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, st.model)),
    S.browser ? h("span", {}, h("i", { class: S.browser.ok ? "ok" : "bad" }),
      S.browser.ok ? "Browser ready" : h("button", { onclick: () => go("settings") }, "Set up a browser")) : null].filter(Boolean));
}
function render() {
  renderChrome();
  const view = { home: viewHome, tests: viewTests, edit: viewEdit, explore: viewExplore, live: viewLive, results: viewResults, result: viewResult, settings: viewSettings }[S.view];
  document.getElementById("main").replaceChildren(view());
}

// ---------------------------------------------------------------- shared pieces
function targetFields(model, onchange) {
  const box = h("div", { class: "stack" });
  const draw = () => {
    box.replaceChildren(
      h("div", { class: "seg", role: "group", "aria-label": "What kind of application" },
        [["web", "Website"], ["cli", "Command line"]].map(([id, label]) =>
          h("button", { type: "button", "aria-pressed": String(model.type === id), onclick: () => { model.type = id; draw(); onchange && onchange(); } }, label))),
      model.type === "web"
        ? h("label", { class: "field" }, "Address",
            h("input", { type: "url", placeholder: "https://your-app.example.com", value: model.url, oninput: e => { model.url = e.target.value; onchange && onchange(); } }))
        : h("label", { class: "field" }, h("span", {}, "Program to keep open ", h("span", { class: "hint" }, "(optional)")),
            h("input", { type: "text", placeholder: "for example: python   or   sqlite3 data.db", value: model.command, oninput: e => { model.command = e.target.value; onchange && onchange(); } }),
            h("span", { class: "hint" }, "Leave empty and Argus runs one-off commands in the workspace folder. Commands run on this computer.")));
  };
  draw();
  return box;
}
function targetOf(model) {
  if (model.type === "web") {
    let url = model.url.trim();
    if (!url) throw new Error("Enter the address of the website to test.");
    if (!/^[a-z]+:\/\//i.test(url)) url = "https://" + url;
    return { type: "web", url };
  }
  return { type: "cli", command: model.command.trim() };
}
function needModel() {
  return modelMissing() ? h("div", { class: "notice" }, h("span", { class: "grow" }, modelMissing()),
    h("button", { class: "btn primary", onclick: () => go("settings") }, "Open Settings")) : null;
}
async function startJob(path, body) {
  try {
    const job = await api("POST", path, body);
    watch(job); go("live");
  } catch (e) {
    if (e.status === 412) { toast(e.message); go("settings"); } else toast(e.message);
  }
}

// ---------------------------------------------------------------- home
function viewHome() {
  const m = S.home;
  const recent = S.results.slice(0, 5);
  return h("div", { class: "page" },
    h("div", {}, h("h1", {}, "What do you want to test?"),
      h("p", { class: "lead" }, "Point Argus at an application and say what it should do, in plain words. Argus opens it, uses it the way a person would, and tells you whether it worked — with the evidence.")),
    needModel(),
    h("div", { class: "card stack" },
      targetFields(m),
      h("label", { class: "field" }, h("span", {}, "What should it do? ", h("span", { class: "hint" }, "One step per line.")),
        h("textarea", { rows: 5, value: m.behaviour, oninput: e => m.behaviour = e.target.value,
          placeholder: "Sign in as \"ann\" with the password \"wrong\"\nAn error message says the password is incorrect\nThe page still shows the sign-in form" })),
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => attempt(async () => startJob("run", { target: targetOf(m), behaviour: m.behaviour })) }, "Run this test"),
        h("button", { class: "btn", onclick: () => attempt(async () => {
          const out = await api("POST", "tests/quick", { target: targetOf(m), behaviour: m.behaviour });
          S.edit = { file: "", content: out.content, check: null }; go("edit");
        }) }, "Save as a test…"),
        h("span", { class: "grow" }),
        h("button", { class: "link", onclick: () => { Object.assign(S.explore, { type: m.type, url: m.url, command: m.command }); go("explore"); } }, "Or let Argus explore it on its own →"))),
    h("div", { class: "loop", "aria-label": "How Argus works" },
      h("span", {}, "Observe"), h("b", {}, "→"), h("span", {}, "Decide"), h("b", {}, "→"), h("span", {}, "Act"), h("b", {}, "→"), h("span", {}, "Verify"),
      h("span", { class: "muted small", style: "background:none;font-weight:400" }, "Argus looks at the app, chooses the next action, performs it, and checks the result.")),
    recent.length ? h("div", {}, h("h2", {}, "Recent results"), resultsTable(recent)) : null);
}

// ---------------------------------------------------------------- saved tests
function viewTests() {
  const rows = S.tests.map(t => h("tr", { class: "click", onclick: () => openTest(t.file) },
    h("td", {}, h("div", { class: "t-name" }, t.name), h("div", { class: "muted small" }, t.error ? "Needs fixing: " + t.error : targetText(t.target) + " · " + t.steps + " steps")),
    h("td", {}, t.error ? badge("error", "Invalid") : t.last ? badge(t.last.status) : h("span", { class: "muted small" }, "Not run yet")),
    h("td", { class: "muted small" }, t.last ? when(t.last.started) : ""),
    h("td", { style: "text-align:right" }, h("button", { class: "btn sm", disabled: !!t.error, onclick: e => { e.stopPropagation(); startJob("run", { files: [t.file] }); } }, "Run"))));
  const runnable = S.tests.filter(t => !t.error).map(t => t.file);
  return h("div", { class: "page" },
    h("div", { class: "row" }, h("div", { class: "grow" }, h("h1", {}, "Saved tests"),
      h("p", { class: "lead" }, "Tests you keep and run again, for example after every change to your app.")),
      h("button", { class: "btn", onclick: () => { S.edit = { file: "", content: "name: \ntarget:\n  type: web\n  url: https://\nsteps:\n  - do: \n", check: null }; go("edit"); } }, "New test"),
      h("button", { class: "btn primary", disabled: !runnable.length, onclick: () => startJob("run", { files: runnable }) }, "Run all")),
    h("div", { class: "card flat" }, S.tests.length
      ? h("table", { class: "list" }, h("thead", {}, h("tr", {}, h("th", {}, "Test"), h("th", {}, "Last result"), h("th", {}, "Last run"), h("th", {}))), h("tbody", {}, rows))
      : h("div", { class: "empty" }, h("p", {}, "No saved tests in this workspace yet."),
          h("div", { class: "row", style: "justify-content:center" },
            h("button", { class: "btn primary", onclick: () => go("home") }, "Test something"),
            h("button", { class: "btn", onclick: () => attempt(async () => { S.tests = await api("POST", "tests/samples"); render(); }) }, "Add two sample tests")))));
}
async function openTest(file) {
  const data = await attempt(() => api("GET", "tests/" + encodeURIComponent(file)));
  if (data) { S.edit = { file: data.file, content: data.content, check: null }; go("edit"); }
}
function viewEdit() {
  const ed = S.edit;
  const status = h("div", {});
  let timer;
  const check = async () => {
    const out = await attempt(() => api("POST", "tests/validate", { content: ed.content }));
    if (!out) return;
    ed.check = out;
    status.replaceChildren(out.ok
      ? h("div", { class: "notice good" }, h("div", {}, h("b", {}, out.name), h("ol", { style: "margin:6px 0 0;padding-left:20px" }, out.steps.map(s => h("li", {}, s))),
          out.needs_model ? h("div", { class: "small muted", style: "margin-top:6px" }, "Uses the model for the plain-language steps.") : h("div", { class: "small muted", style: "margin-top:6px" }, "Exact steps only: runs without a model.")))
      : h("div", { class: "notice bad" }, out.error));
  };
  const area = h("textarea", { class: "code", spellcheck: "false", value: ed.content, "aria-label": "Test file",
    oninput: e => { ed.content = e.target.value; clearTimeout(timer); timer = setTimeout(check, 400); } });
  check();
  const describe = h("textarea", { rows: 3, placeholder: "Describe the behaviour, e.g. “The search box on https://example.com finds products by name and shows a message when nothing matches.”" });
  const draftBtn = h("button", { class: "btn", onclick: async () => {
    if (!describe.value.trim()) return toast("Describe the behaviour first.");
    draftBtn.disabled = true; draftBtn.textContent = "Writing…";
    const out = await attempt(() => api("POST", "draft", { description: describe.value }));
    draftBtn.disabled = false; draftBtn.textContent = "Write the test for me";
    if (out) { ed.content = out.content; area.value = out.content; check(); toast("Draft written. Review it before saving."); }
  } }, "Write the test for me");
  const save = async () => {
    const out = await api("POST", "tests", { file: ed.file, content: ed.content });
    ed.file = out.file; S.tests = out.tests; toast("Saved as " + out.file); render(); return out.file;
  };
  return h("div", { class: "page" },
    h("div", { class: "row" }, h("button", { class: "btn sm", onclick: () => go("tests") }, "← Saved tests"),
      h("h1", { class: "grow", style: "margin:0" }, ed.file || "New test"),
      ed.file ? h("button", { class: "btn danger", onclick: () => modal("Delete " + ed.file + "?", h("p", {}, "The test file is removed from the workspace. Results of earlier runs are kept."),
        [{ label: "Delete", kind: "danger", run: async close => { S.tests = await api("DELETE", "tests/" + encodeURIComponent(ed.file)); close(); go("tests"); } }]) }, "Delete") : null,
      h("button", { class: "btn", onclick: () => attempt(save) }, "Save"),
      h("button", { class: "btn primary", onclick: () => attempt(async () => { const file = await save(); await startJob("run", { files: [file] }); }) }, "Save and run")),
    h("div", { class: "grid2" },
      h("div", { class: "stack" }, area),
      h("div", { class: "stack" }, status,
        h("div", { class: "card stack" }, h("h3", {}, "Let the model write it"), describe, h("div", {}, draftBtn),
          modelMissing() ? h("span", { class: "small muted" }, "Needs a model — set one up in Settings.") : null),
        h("details", { class: "ref" }, h("summary", {}, "File format"), h("pre", {}, S.state.reference)))));
}

// ---------------------------------------------------------------- explore
function viewExplore() {
  const m = S.explore;
  return h("div", { class: "page" },
    h("div", {}, h("h1", {}, "Explore"),
      h("p", { class: "lead" }, "No script. Argus roams the application like a curious user — tries the features, feeds it awkward input — and reports the defects it actually ran into.")),
    needModel(),
    h("div", { class: "card stack" },
      targetFields(m),
      h("label", { class: "field" }, h("span", {}, "What to focus on ", h("span", { class: "hint" }, "(optional)")),
        h("textarea", { rows: 3, value: m.goal, oninput: e => m.goal = e.target.value, placeholder: "For example: the checkout form, especially invalid card numbers" })),
      h("label", { class: "field", style: "max-width:220px" }, "Stop after this many actions",
        h("input", { type: "number", min: 1, max: 300, value: m.budget, oninput: e => m.budget = e.target.value })),
      h("div", {}, h("button", { class: "btn primary", onclick: () => attempt(async () => startJob("explore", { target: targetOf(m), goal: m.goal, budget: Number(m.budget) })) }, "Start exploring"))));
}

// ---------------------------------------------------------------- live
function watch(job) {
  S.live = { job: job.id, kind: job.kind, title: job.title, running: true, phase: "", runs: [], current: null, shot: null, output: "", logs: [], results: [] };
  pump(job.id, 0);
}
async function pump(jobId, since) {
  while (S.live && S.live.job === jobId) {
    let data;
    try { data = await api("GET", "events?job=" + jobId + "&since=" + since); }
    catch (e) { S.live.running = false; S.live.logs.push(e.message); render(); return; }
    if (!data.job) return;
    data.events.forEach(applyEvent);
    since = data.next;
    S.live.running = data.job.running;
    if (!data.job.running) { await finishLive(); return; }
    if (S.view === "live") render(); else renderChrome();
  }
}
async function finishLive() {
  await attempt(async () => { [S.tests, S.results] = await Promise.all([api("GET", "tests"), api("GET", "results")]); });
  S.live.running = false; S.live.phase = "";
  render();
}
function applyEvent(ev) {
  const L = S.live, run = L.current;
  switch (ev.type) {
    case "test_start":
      L.current = { id: ev.id, kind: "test", name: ev.name, target: ev.target, status: "running", summary: "",
        steps: ev.steps.map((s, i) => ({ index: i, text: s.text, kind: s.kind, status: "pending", detail: "", actions: [] })) };
      L.runs.push(L.current); L.output = ""; L.shot = null; break;
    case "explore_start":
      L.current = { id: ev.id, kind: "explore", name: ev.name, target: ev.target, status: "running", summary: "", actions: [], findings: [], budget: ev.budget };
      L.runs.push(L.current); break;
    case "step_start": run.steps[ev.index].status = "running"; break;
    case "step_end": Object.assign(run.steps[ev.index], { status: ev.status, detail: ev.detail, judged_by: ev.judged_by, shot: ev.shot }); L.phase = ""; break;
    case "action": (ev.index >= 0 ? run.steps[ev.index].actions : run.actions).push(ev); break;
    case "finding": run.findings.push(ev); break;
    case "phase": L.phase = ev.phase; break;
    case "screenshot": if (run) L.shot = { id: run.id, file: ev.file }; break;
    case "output": L.output = ev.text; break;
    case "log": L.logs.push(ev.text); break;
    case "test_end": case "explore_end":
      Object.assign(run, { status: ev.status, summary: ev.summary, end_reason: ev.end_reason });
      run.steps && run.steps.forEach(s => { if (s.status === "pending") s.status = "skipped"; if (s.status === "running") s.status = ev.status === "stopped" ? "stopped" : "error"; });
      break;
    case "job_end": L.results = ev.results; break;
  }
}
function loopBar(phase) {
  const names = [["observe", "Observe"], ["decide", "Decide"], ["act", "Act"], ["verify", "Verify"]];
  return h("div", { class: "loop", "aria-label": "Current stage" },
    names.flatMap(([id, label], i) => [i ? h("b", {}, "→") : null, h("span", { class: phase === id ? "on" : "" }, label)]));
}
function stepList(run, pick) {
  return h("ol", { class: "steps" }, run.steps.map(s => h("li", {},
    h("div", { class: "step-head" }, h("span", { class: "mark " + s.status, title: WORD[s.status] }, MARK[s.status] || s.index + 1),
      h("div", { class: "grow" }, h("div", { class: "step-text" }, s.text),
        s.judged_by ? h("div", { class: "muted small" }, s.judged_by === "model" ? "Judged by the model" : "Exact check") : null),
      s.shot && pick ? h("button", { class: "link small", onclick: () => pick(s.shot) }, "view") : null),
    s.actions && s.actions.length ? actionList(s.actions, pick) : null,
    s.detail ? h("div", { class: "step-detail " + s.status }, s.detail) : null)));
}
function actionList(actions, pick) {
  return h("ul", { class: "acts" }, actions.map(a => h("li", { class: a.ok ? "" : "bad" },
    a.why ? h("div", { class: "why" }, a.why) : null,
    h("div", {}, h("b", {}, a.label), a.shot && pick ? h("button", { class: "link", onclick: () => pick(a.shot) }, "view") : null),
    h("div", { class: "res" }, (a.ok ? "→ " : "✕ ") + a.result))));
}
function findingList(findings) {
  if (!findings.length) return h("div", { class: "empty" }, "No findings.");
  return h("div", {}, findings.map(f => h("div", { class: "finding" },
    h("div", { class: "row" }, badge(f.severity, f.severity), h("b", { class: "grow" }, f.title),
      f.source === "browser" ? h("span", { class: "muted small" }, "reported by the browser") : null),
    f.detail && f.detail !== f.title ? h("p", {}, f.detail) : null)));
}
function seePane(run, shot, output) {
  const isWeb = run.target && run.target.type === "web";
  return h("div", { class: "card flat" },
    h("div", { class: "view-title" }, isWeb ? "What Argus sees" : "Terminal", h("span", { class: "muted small grow", style: "font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, targetText(run.target))),
    isWeb
      ? h("div", { class: "screen" }, shot ? h("img", { src: "/api/results/" + shot.id + "/file/" + shot.file, alt: "Screenshot of the page under test" }) : h("span", { class: "muted" }, "No screenshot yet"))
      : h("pre", { class: "term", tabindex: "0" }, output || "(no output yet)"));
}
function verdict(run) {
  const title = run.kind === "explore" ? (WORD[run.status] || run.status) : (WORD[run.status] || run.status);
  return h("div", { class: "verdict " + run.status }, h("strong", {}, title), h("span", { class: "grow" }, run.summary,
    run.end_reason ? h("div", { class: "small muted" }, run.end_reason) : null));
}
function viewLive() {
  const L = S.live;
  if (!L) return h("div", { class: "page" }, h("div", { class: "card empty" }, "Nothing is running."));
  const run = L.current;
  const head = h("div", { class: "row" },
    h("div", { class: "grow" }, h("h1", {}, L.title), L.running ? loopBar(L.phase) : null),
    L.running ? h("button", { class: "btn danger", onclick: () => attempt(() => api("POST", "stop")) }, "Stop")
      : h("button", { class: "btn", onclick: () => go(L.kind === "explore" ? "explore" : "home") }, "Done"));
  if (!run) return h("div", { class: "page wide" }, head, h("div", { class: "card empty" }, L.logs.slice(-1)[0] || "Starting…"));
  const others = L.runs.length > 1 ? h("div", { class: "card flat" }, h("table", { class: "list" }, h("tbody", {},
    L.runs.map(r => h("tr", { class: "click", onclick: () => !L.running && openResult(r.id) }, h("td", { class: "t-name" }, r.name), h("td", {}, badge(r.status)), h("td", { class: "muted small" }, r.summary)))))) : null;
  const left = run.kind === "explore"
    ? [h("div", { class: "card flat" }, h("div", { class: "view-title" }, "Findings", h("span", { class: "badge" }, run.findings.length)), findingList(run.findings)),
       h("div", { class: "card flat" }, h("div", { class: "view-title" }, "Actions", h("span", { class: "badge" }, run.actions.length + " of " + run.budget)),
         run.actions.length ? h("div", { style: "padding:4px 16px 14px 0" }, actionList(run.actions.slice(-40))) : h("div", { class: "empty" }, "Nothing done yet."))]
    : [h("div", { class: "card flat" }, h("div", { class: "view-title" }, run.name), stepList(run))];
  return h("div", { class: "page wide" }, head,
    !L.running && run.status !== "running" ? h("div", { class: "row" }, h("div", { class: "grow" }, verdict(run)),
      h("button", { class: "btn primary", onclick: () => openResult(run.id) }, "Open the full result")) : null,
    others,
    h("div", { class: "live" }, h("div", { class: "stack" }, left), h("div", { class: "stack" }, seePane(run, L.shot, L.output),
      L.logs.length ? h("div", { class: "muted small" }, L.logs.slice(-3).map(t => h("div", {}, t))) : null)));
}

// ---------------------------------------------------------------- results
function resultsTable(list) {
  return h("div", { class: "card flat" }, h("table", { class: "list" },
    h("thead", {}, h("tr", {}, h("th", {}, "What"), h("th", {}, "Outcome"), h("th", {}, "Detail"), h("th", {}, "When"))),
    h("tbody", {}, list.map(r => h("tr", { class: "click", onclick: () => openResult(r.id) },
      h("td", {}, h("div", { class: "t-name" }, r.name), h("div", { class: "muted small" }, (r.kind === "explore" ? "Exploration" : "Test") + " · " + targetText(r.target))),
      h("td", {}, badge(r.status)),
      h("td", { class: "small muted" }, r.kind === "explore" ? r.findings + " finding" + (r.findings === 1 ? "" : "s") : r.passed_steps + " of " + r.total_steps + " steps passed"),
      h("td", { class: "small muted" }, when(r.started)))))));
}
function viewResults() {
  return h("div", { class: "page" }, h("div", {}, h("h1", {}, "Results"), h("p", { class: "lead" }, "Every run and exploration in this workspace, with what Argus did and saw.")),
    S.results.length ? resultsTable(S.results) : h("div", { class: "card empty" }, "Nothing has been run yet."));
}
async function openResult(id) {
  const data = await attempt(() => api("GET", "results/" + id));
  if (!data) return;
  S.result = data; S.shot = null; S.transcript = "";
  if (data.transcript) { try { S.transcript = await (await fetch("/api/results/" + id + "/file/" + data.transcript)).text(); } catch (e) { /* shown as empty */ } }
  go("result");
}
function viewResult() {
  const r = S.result;
  const pick = file => { S.shot = { id: r.id, file }; render(); };
  let shot = S.shot;
  if (!shot && r.target.type === "web") {
    const all = r.kind === "explore" ? r.actions.map(a => a.shot) : r.steps.flatMap(s => [...s.actions.map(a => a.shot), s.shot]);
    const last = all.filter(Boolean).pop();
    if (last) shot = { id: r.id, file: last };
  }
  const tokens = r.tokens && r.tokens.calls ? r.tokens.calls + " model calls · " + (r.tokens.input + r.tokens.output).toLocaleString() + " tokens" : "No model calls";
  const left = r.kind === "explore"
    ? [h("div", { class: "card flat" }, h("div", { class: "view-title" }, "Findings", h("span", { class: "badge" }, r.findings.length)), findingList(r.findings)),
       h("div", { class: "card flat" }, h("div", { class: "view-title" }, "What Argus did", h("span", { class: "badge" }, r.actions.length)),
         r.actions.length ? h("div", { style: "padding:4px 16px 14px 0" }, actionList(r.actions, pick)) : h("div", { class: "empty" }, "No actions were performed."))]
    : [h("div", { class: "card flat" }, h("div", { class: "view-title" }, "Steps"), stepList(r, pick))];
  return h("div", { class: "page wide" },
    h("div", { class: "row" }, h("button", { class: "btn sm", onclick: () => go("results") }, "← Results"),
      h("h1", { class: "grow", style: "margin:0" }, r.name),
      r.kind === "test" && !r.file && r.source ? h("button", { class: "btn", onclick: () => { S.edit = { file: "", content: r.source, check: null }; go("edit"); } }, "Save as a test…") : null,
      r.kind === "test" ? h("button", { class: "btn", onclick: () => startJob("run", r.file ? { files: [r.file] } : { content: r.source }) }, "Run again") : null,
      h("button", { class: "btn danger", onclick: () => attempt(async () => { S.results = await api("DELETE", "results/" + r.id); go("results"); }) }, "Delete")),
    verdict(r),
    h("div", { class: "muted small" }, [when(r.started), secs(r.duration), r.model || null, tokens].filter(Boolean).join(" · ")),
    h("div", { class: "live" }, h("div", { class: "stack" }, left), h("div", { class: "stack" }, seePane(r, shot, S.transcript))));
}

// ---------------------------------------------------------------- settings
function viewSettings() {
  const st = S.state.settings;
  const active = st.providers.find(p => p.id === st.provider);
  const choice = { provider: st.provider || "anthropic", model: st.model };
  const outcome = h("div", { class: "result-line" });
  const modelInput = h("input", { type: "text", list: "model-list", value: choice.model, placeholder: "Choose or type a model name", oninput: e => choice.model = e.target.value.trim() });
  const dataList = h("datalist", { id: "model-list" });
  const fillModels = () => {
    const p = st.providers.find(x => x.id === choice.provider);
    dataList.replaceChildren(...(p.models || []).map(m => h("option", { value: m })));
    modelInput.placeholder = p.models && p.models.length ? "Choose from " + p.models.length + " models, or type a name" : (p.suggested_model ? "e.g. " + p.suggested_model : "Press “Find models”, or type a name");
  };
  fillModels();
  const busy = async (btn, label, fn) => { const old = btn.textContent; btn.disabled = true; btn.textContent = label; try { await fn(); } finally { btn.disabled = false; btn.textContent = old; } };
  const findBtn = h("button", { class: "btn", onclick: () => busy(findBtn, "Looking…", async () => {
    const out = await attempt(() => api("POST", "provider/test", { provider: choice.provider }));
    if (!out) return;
    if (!out.ok) { outcome.className = "result-line bad"; outcome.textContent = out.error; return; }
    S.state.settings = out.settings; st.providers = out.settings.providers; fillModels();
    outcome.className = "result-line good"; outcome.textContent = "Connected. " + out.models.length + " models available — pick one.";
    if (!choice.model) { const p = st.providers.find(x => x.id === choice.provider); if (p.suggested_model && out.models.includes(p.suggested_model)) { choice.model = p.suggested_model; modelInput.value = choice.model; } }
    modelInput.focus();
  }) }, "Find models");
  const useBtn = h("button", { class: "btn primary", onclick: () => busy(useBtn, "Testing…", async () => {
    if (!choice.model) { outcome.className = "result-line bad"; outcome.textContent = "Choose a model first."; return; }
    const out = await attempt(() => api("POST", "provider/test", { provider: choice.provider, model: choice.model }));
    if (!out) return;
    if (!out.ok) { outcome.className = "result-line bad"; outcome.textContent = out.error; return; }
    S.state.settings = await api("POST", "settings", { provider: choice.provider, model: choice.model });
    toast("Model saved: it answered “" + out.reply + "”."); render();
  }) }, "Test and use this model");

  const provCard = p => {
    const key = h("input", { type: "password", autocomplete: "off", placeholder: p.key_set ? "Saved: " + p.key_hint + (p.key_source === "environment" ? " (from " + p.env + ")" : "") : "Paste the API key" });
    const url = h("input", { type: "text", value: p.base_url, placeholder: p.default_base_url || "https://…/v1" });
    const save = async body => { const out = await attempt(() => api("POST", "provider/save", { provider: p.id, ...body })); if (out) { S.state.settings = out; toast("Saved."); render(); } };
    return h("div", { class: "prov" + (st.provider === p.id ? " active" : "") },
      h("div", { class: "prov-head" }, h("b", {}, p.label),
        p.missing ? badge("warn", p.needs_key ? "Needs a key" : "Needs an address") : badge("ok", p.key_source === "environment" ? "Key from " + p.env : p.key_set ? "Key saved" : "Ready"),
        st.provider === p.id ? badge("running", "In use") : null),
      h("div", { class: "muted small" }, p.key_help),
      p.needs_key || p.id === "custom" ? h("div", { class: "row" }, h("div", { class: "grow" }, key),
        h("button", { class: "btn", onclick: () => key.value.trim() ? save({ api_key: key.value }) : toast("Paste the key first.") }, "Save key"),
        p.key_source === "saved" ? h("button", { class: "btn danger", onclick: () => save({ api_key: "" }) }, "Remove") : null) : null,
      p.base_url_editable ? h("label", { class: "field" }, h("span", {}, "Server address ", h("span", { class: "hint" }, p.default_base_url ? "(change only if you use a proxy or another host)" : "")),
        h("div", { class: "row" }, h("div", { class: "grow" }, url), h("button", { class: "btn", onclick: () => save({ base_url: url.value }) }, "Save address"))) : null);
  };

  const browserBox = h("div", { class: "stack" });
  const drawBrowser = () => {
    const b = S.browser;
    if (!b) return browserBox.replaceChildren(h("span", { class: "muted" }, "Checking…"));
    const btn = h("button", { class: "btn", onclick: () => busy(btn, "Downloading… this can take a minute", async () => {
      const out = await attempt(() => api("POST", "browser/install"));
      if (out) { S.browser = out.status; toast(out.ok ? "Chromium installed." : "Install failed."); if (!out.ok) modal("Browser install failed", h("pre", { style: "white-space:pre-wrap" }, out.detail)); drawBrowser(); renderChrome(); }
    }) }, "Install Chromium for Argus");
    browserBox.replaceChildren(h("div", { class: "row" }, badge(b.ok ? "ok" : "warn", b.ok ? "Ready" : "Not ready"), h("span", { class: "grow" }, b.detail), b.can_install ? btn : null));
  };
  drawBrowser();
  const opt = (key, label, hint, invert) => h("label", { class: "check" },
    h("input", { type: "checkbox", checked: invert ? !st[key] : st[key], onchange: async e => { const out = await attempt(() => api("POST", "settings", { [key]: invert ? !e.target.checked : e.target.checked })); if (out) S.state.settings = out; } }),
    h("span", {}, h("b", {}, label), h("div", { class: "muted small" }, hint)));
  const maxInput = h("input", { type: "number", min: 1, max: 100, value: st.max_actions, style: "max-width:110px",
    onchange: async e => { const out = await attempt(() => api("POST", "settings", { max_actions: Number(e.target.value) })); if (out) { S.state.settings = out; toast("Saved."); } } });

  return h("div", { class: "page" },
    h("div", {}, h("h1", {}, "Settings"), h("p", { class: "lead" }, "Everything Argus needs is set here. Nothing has to be edited in files.")),
    st.load_error ? h("div", { class: "notice bad" }, st.load_error) : null,
    h("div", { class: "card stack" },
      h("h2", {}, "Model"),
      h("p", { class: "muted", style: "margin:0" }, "Argus uses a model to look at your application, decide what to do next and judge the outcome. ",
        active && !st.model_missing ? h("b", {}, "In use: " + active.label + " · " + st.model) : h("b", {}, st.model_missing)),
      h("div", { class: "grid2" },
        h("label", { class: "field" }, "Provider", h("select", { onchange: e => { choice.provider = e.target.value; choice.model = ""; modelInput.value = ""; outcome.textContent = ""; fillModels(); } },
          st.providers.map(p => h("option", { value: p.id, selected: p.id === choice.provider }, p.label + (p.missing ? " — not set up" : ""))))),
        h("label", { class: "field" }, "Model", modelInput, dataList)),
      h("div", { class: "row" }, findBtn, useBtn), outcome),
    h("div", { class: "card stack" }, h("h2", {}, "Providers and API keys"),
      h("p", { class: "muted small", style: "margin:0" }, "Keys are stored on this computer in ", h("code", {}, st.file), ", readable only by your account. They are never shown again and never written into a workspace."),
      h("div", { class: "grid2" }, st.providers.map(provCard))),
    h("div", { class: "card stack" }, h("h2", {}, "Browser for website tests"), browserBox),
    h("div", { class: "card stack" }, h("h2", {}, "How Argus runs"),
      opt("vision", "Send screenshots to the model", "Lets the model see the page, not only read it. Turn off for text-only models or to spend fewer tokens."),
      opt("headless", "Show the browser window while testing", "Watch the real browser being driven. The live view in Argus works either way.", true),
      h("label", { class: "field" }, h("span", {}, "Actions allowed per plain-language step ", h("span", { class: "hint" }, "(a step fails if it needs more)")), maxInput)),
    h("div", { class: "card stack" }, h("h2", {}, "Workspace"),
      h("div", { class: "row" }, h("code", { class: "grow" }, S.state.workspace), h("button", { class: "btn", onclick: pickWorkspace }, "Change folder…")),
      h("p", { class: "muted small", style: "margin:0" }, "Tests are saved in .argus/tests and results in .argus/results inside this folder. Command-line tests run here.")),
    h("p", { class: "muted small" }, "Argus " + S.state.version));
}
function pickWorkspace() {
  const list = h("div", { class: "folders" });
  const pathInput = h("input", { type: "text", "aria-label": "Folder path" });
  let current = S.state.workspace;
  const load = async path => {
    const out = await attempt(() => api("GET", "browse?path=" + encodeURIComponent(path)));
    if (!out) return;
    current = out.path; pathInput.value = out.path;
    list.replaceChildren(out.parent ? h("button", { onclick: () => load(out.parent) }, "↑ Up one level") : null,
      ...out.folders.map(f => h("button", { onclick: () => load(out.path + (/[\\/]$/.test(out.path) ? "" : "/") + f) }, "📁 " + f)),
      !out.folders.length ? h("div", { class: "empty" }, "No folders inside.") : null);
  };
  pathInput.addEventListener("keydown", e => { if (e.key === "Enter") load(pathInput.value); });
  const recent = S.state.settings.recent_workspaces.filter(p => p !== S.state.workspace);
  modal("Choose the workspace folder", [pathInput, list,
    recent.length ? h("div", { class: "small" }, h("span", { class: "muted" }, "Recent: "), recent.map(p => h("button", { class: "link", style: "margin-right:10px", onclick: () => load(p) }, p))) : null],
    [{ label: "Use this folder", kind: "primary", run: async close => {
      const out = await attempt(() => api("POST", "workspace", { path: pathInput.value || current }));
      if (out) { close(); await refresh(); toast("Workspace changed."); render(); }
    } }]);
  load(current);
}
document.getElementById("ws-btn").addEventListener("click", pickWorkspace);

// ---------------------------------------------------------------- start
(async function start() {
  try { await refresh(); }
  catch (e) { document.getElementById("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "notice bad" }, e.message))); return; }
  if (S.state.job) { watch(S.state.job); if (S.state.job.running) S.view = "live"; }
  render();
  loadBrowser();
})();
