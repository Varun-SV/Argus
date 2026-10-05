/* Argus desktop app — a conversation with Argus.
 *
 * Typed messages go to the Python API (window.pywebview.api.interpret), which
 * returns one intent. Buttons, chips and menus skip that step and run their
 * intent directly. Every result is a message in the thread; long-running
 * work (runs, roam, watch) is polled until it finishes.
 *
 * All text from the engine, the model or the user is rendered with
 * textContent. Only the constant icon markup below is parsed as HTML.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

/* ------------------------------------------------------------- icons --- */
const ICON = {
  logo: '<svg width="22" height="22" viewBox="0 0 100 100" fill="none" aria-hidden="true"><circle cx="50" cy="50" r="20" stroke="#D97757" stroke-width="9"/><path d="M50 40l9 5v10l-9 5-9-5V45z" fill="#D97757"/></svg>',
  logoBig: '<svg width="40" height="40" viewBox="0 0 100 100" fill="none" aria-hidden="true"><circle cx="50" cy="50" r="20" stroke="#D97757" stroke-width="8"/><path d="M50 40l9 5v10l-9 5-9-5V45z" fill="#D97757"/></svg>',
  flask: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3D3D3A" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3"/></svg>',
  compass: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3D3D3A" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/></svg>',
  file: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#87867F" stroke-width="1.7" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
  fileDark: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#5E5D59" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></svg>',
  pass: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3F6B30" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
  fail: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#B42318" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  active: '<svg class="spin" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="#E8E6DC" stroke-width="3"/><path d="M12 4a8 8 0 0 1 8 8" fill="none" stroke="#D97757" stroke-width="3" stroke-linecap="round"/></svg>',
  pending: '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="6" fill="none" stroke="#C2C0B6" stroke-width="2"/></svg>',
  skipped: '<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 12h10" stroke="#9C9A92" stroke-width="2.4" stroke-linecap="round"/></svg>',
  shieldOk: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#3F6B30" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg>',
  shieldBad: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#B42318" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M9.5 9.5l5 5M14.5 9.5l-5 5"/></svg>',
  check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#D97757" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
};

/* --------------------------------------------------------------- DOM --- */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  // Menus live inside the composer <form>; a default (submit) button there would
  // be "clicked" by pressing Enter in the input.
  if (tag === "button") el.type = "button";
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "icon") el.insertAdjacentHTML("afterbegin", ICON[v]);
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return el;
}
const iconEl = (name, cls) => h("span", { class: cls, icon: name });
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const pad = (n) => (n < 10 ? "0" : "") + n;
const mmss = (s) => pad(Math.floor(Math.max(0, s) / 60)) + ":" + pad(Math.floor(Math.max(0, s) % 60));
const adapterShort = (a) => ({ "desktop-gui": "Desktop", browser: "Browser", cli: "CLI" }[a] || a || "—");
const STATUS_LABEL = { queued: "Queued", running: "Running", pass: "Passed", fail: "Failed", error: "Error",
  stopped: "Stopped", done: "Finished", skipped: "Skipped", unknown: "Outcome unknown" };

/* ------------------------------------------------------------- state --- */
const state = {
  info: null,
  tests: [],
  conversations: [],
  conv: null,
  menu: null,
  liveTs: 0,
  liveJob: null,
  pollTimer: null,
  pollInFlight: false,
  pollRequested: false,
  pollDone: null,
  finishPoll: null,
  saveTimer: null,
  saveChain: Promise.resolve(),
  closing: false,
  pendingActions: 0,
  watchOn: false,
  watchSettled: null,
};
const nodeCache = new Map();

function newConversation() {
  return { id: "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    title: "", updated: Date.now(), msgs: [], followups: [], seq: 1 };
}
// Every write names the conversation it belongs to, so an answer that arrives
// after the user switched conversations lands where it was asked.
function pushIn(conv, msg) {
  msg.id = conv.id + ":" + conv.seq++;
  msg.v = 1;
  conv.msgs.push(msg);
  conv.updated = Date.now();
  if (conv === state.conv) renderThread();
  return msg;
}
function update(msg, patch) {
  Object.assign(msg, patch || {});
  msg.v = (msg.v || 0) + 1;
  renderThread();
}
function removeIn(conv, msg) {
  const i = conv.msgs.indexOf(msg);
  if (i >= 0) conv.msgs.splice(i, 1);
  if (conv === state.conv) renderThread();
}
const sayIn = (conv, text, extra) => pushIn(conv, Object.assign({ role: "argus", kind: "text", text }, extra || {}));
function followupsIn(conv, list) {
  conv.followups = list || [];
  if (conv === state.conv) renderFollowups();
}
async function thinkingIn(conv, fn) {
  const thinking = pushIn(conv, { role: "argus", kind: "thinking" });
  try { return await fn(); } finally { removeIn(conv, thinking); }
}
// Conversation-bound helpers used by every action below.
function inConv(conv) {
  return {
    push: (m) => pushIn(conv, m),
    say: (t, x) => sayIn(conv, t, x),
    sayError: (t) => sayIn(conv, t, { error: true }),
    setFollowups: (l) => followupsIn(conv, l),
    withThinking: (fn) => thinkingIn(conv, fn),
  };
}
const push = (m) => pushIn(state.conv, m);
const setFollowups = (l) => followupsIn(state.conv, l);

/* ------------------------------------------------------------ intents --- */
const I = (intent, args) => ({ intent, args: args || {} });

async function sendText(text) {
  if (state.closing) return;
  text = (text || "").trim();
  if (!text) return;
  const conv = state.conv;
  if (!conv.title) { conv.title = text.slice(0, 60); renderRecents(); }
  pushIn(conv, { role: "user", kind: "user", text });
  followupsIn(conv, []);
  let res;
  try {
    res = await thinkingIn(conv, () => api().interpret(text));
  } catch (e) {
    res = I("error", { text: String(e) });
  }
  await execute(res, conv);
}

async function runChip(label, intentObj) {
  if (state.closing) return;
  const conv = state.conv;
  if (!conv.title) { conv.title = label.slice(0, 60); renderRecents(); }
  pushIn(conv, { role: "user", kind: "user", text: label });
  followupsIn(conv, []);
  await execute(intentObj, conv);
}

async function execute(res, conv = state.conv) {
  if (state.closing) return;
  state.pendingActions++;
  const a = res.args || {};
  const { push, say, sayError, setFollowups, withThinking } = inConv(conv);
  try {
    switch (res.intent) {
      case "none": return;
      case "error": sayError(a.text || "Something went wrong."); return;
      case "chat": say(a.reply || ""); return;
      case "help": {
        const r = await api().help();
        push({ role: "argus", kind: "help", groups: r.groups });
        setFollowups([
          { label: "Run all tests", intent: I("run", { tests: "all" }) },
          { label: "Check my provider connection and vision", intent: I("providers") },
          { label: "Show recent runs", intent: I("report") },
        ]);
        return;
      }
      case "retry_settings": {
        await refreshInfo();
        await refreshTests();
        if (state.info && state.info.ok) say("Project settings loaded. You're ready to continue.");
        return;
      }
      case "stop": {
        await api().stop();
        say("Stopping at the next step. Anything already observed is kept in the report and the knowledge graph.");
        poll();
        return;
      }
      case "run": return await startRun(a.tests || "all", pick(a, ["environment", "capsule_provider", "retain"]), conv);
      case "dry_run": {
        const r = await api().dry_run(a.tests || "all", a.draft || null);
        if (!r.ok) return sayError(r.error);
        push({ role: "argus", kind: "dry", items: r.items });
        setFollowups(a.tests === "draft"
          ? [{ label: "Save it to .argus", intent: I("save_test", { draft: a.draft || null }) }]
          : [{ label: "Run all tests", intent: I("run", { tests: "all" }) }, { label: "Watch for changes", intent: I("watch", { action: "start" }) }]);
        return;
      }
      case "roam": return await startRoam(a, conv);
      case "write_test": {
        const draft = await withThinking(() => api().draft_test(a.description || ""));
        showDraft(draft, conv);
        return;
      }
      case "save_test": {
        const r = await api().save_test(a.draft || null);
        if (!r.ok) return sayError(r.error);
        markDraftSaved(r.draft_id);
        say(`Saved to ${r.path}. It now runs with the rest of the suite, and watch mode picks it up on every change.`);
        await refreshTests();
        setFollowups([{ label: `Run ${r.file}`, intent: I("run", { tests: [r.file] }) }, { label: "Dry run all", intent: I("dry_run", { tests: "all" }) }]);
        return;
      }
      case "explain": {
        // After a restart the API has no in-memory result for a card's key, so send the
        // card's own saved result along; the API only uses it when the key is unknown.
        const card = a.key ? allConversations().flatMap((c) => c.msgs)
          .find((m) => m.kind === "run" && m.snap && m.snap.key === a.key) : null;
        const restored = card && card.snap.result ? card.snap.result : null;
        const r = await withThinking(() => api().explain(a.key || null, restored));
        if (!r.ok) return sayError(r.error);
        say(r.text);
        setFollowups([
          { label: "Re-run in a Capsule and keep the Failure Capsule", intent: I("run", { tests: [r.test], environment: "capsule", retain: true }) },
          // Evidence of the run just explained; historical results can't be verified here.
          r.evidence_key ? { label: "Show ATES evidence", intent: I("evidence", { key: r.evidence_key }) } : null,
          { label: "Run history", intent: I("report") },
        ].filter(Boolean));
        return;
      }
      case "knowledge": return await knowledge(a.action || "show", a.target || "", conv);
      case "evidence": {
        const r = await withThinking(() => api().evidence(a.key || null));
        if (!r.ok) return sayError(r.error);
        push({ role: "argus", kind: "evidence", e: r });
        setFollowups([{ label: "Run history", intent: I("report") }, { label: "Token usage", intent: I("tokens") }]);
        return;
      }
      case "report": {
        const rows = await api().recent_runs(8);
        if (!rows.length) return say("No runs yet. Run a test and its result shows up here.");
        push({ role: "argus", kind: "report", rows });
        const failedRow = rows.find((r) => r.status !== "pass");
        setFollowups(failedRow
          ? [{ label: `Why did ${failedRow.test} fail?`, intent: I("explain", { key: failedRow.id }) }, { label: "Token usage", intent: I("tokens") }]
          : [{ label: "Token usage", intent: I("tokens") }]);
        return;
      }
      case "tokens": {
        const r = await api().token_usage();
        push({ role: "argus", kind: "tokens", usage: r });
        setFollowups([{ label: "Run history", intent: I("report") }, { label: "Check my provider connection and vision", intent: I("providers") }]);
        return;
      }
      case "providers": {
        const r = await withThinking(() => api().check_provider());
        push({ role: "argus", kind: "providers", status: r });
        setFollowups([{ label: "Token usage", intent: I("tokens") }, { label: "Run all tests", intent: I("run", { tests: "all" }) }]);
        return;
      }
      case "switch_provider": return await switchProvider(a.provider, conv);
      case "environment": {
        if (a.environment) {
          const r = await api().set_environment(a.environment, a.capsule_provider || null);
          if (!r.ok) return sayError(r.error);
        }
        if (typeof a.retain === "boolean") await api().set_retain(a.retain);
        await refreshInfo();
        const env = await api().environment();
        push({ role: "argus", kind: "env", env });
        setFollowups(env.environment === "capsule"
          ? [{ label: "Run all tests", intent: I("run", { tests: "all" }) }, { label: "Switch back to local", intent: I("environment", { environment: "local" }) }]
          : [{ label: "Use a Capsule for runs", intent: I("environment", { environment: "capsule", capsule_provider: "auto" }) }]);
        return;
      }
      case "init": {
        const r = await api().init_project();
        push({ role: "argus", kind: "init", files: r.files });
        await refreshInfo(); await refreshTests();
        setFollowups([{ label: "Check my provider connection and vision", intent: I("providers") }, { label: "Dry run all", intent: I("dry_run", { tests: "all" }) }]);
        return;
      }
      case "watch": {
        if (a.action === "stop") {
          await api().watch_stop();
          state.watchOn = false;
          say("Stopped watching .argus/.");
          poll();
          return;
        }
        const r = await api().watch_start();
        if (!r.ok) return sayError(r.error);
        state.watchOn = true;
        state.watchSettled = 0;
        push({ role: "argus", kind: "watch", watch: r.watch });
        setFollowups([{ label: "Stop watching", intent: I("watch", { action: "stop" }) }]);
        poll();
        return;
      }
      default:
        sayError(`I don't know how to do “${res.intent}” yet. Type /help.`);
    }
  } catch (e) {
    sayError(String(e && e.message ? e.message : e));
  } finally {
    state.pendingActions--;
    scheduleSave();
  }
}
function pick(obj, keys) {
  const out = {};
  for (const k of keys) if (obj[k] !== undefined) out[k] = obj[k];
  return out;
}

async function startRun(tests, overrides, conv = state.conv) {
  const { push, sayError, setFollowups } = inConv(conv);
  const r = await api().run_tests(tests, overrides || {});
  if (!r.ok) return sayError(r.error);
  const job = r.job;
  const cards = job.runs.map((run, idx) => push({ role: "argus", kind: "run", job: job.id, idx, snap: run, meta: runMeta(job) }));
  // A quick run can finish before run_tests returns; polling never sees it change, so finish it now.
  if (!job.running) return onFinished(cards.length ? [[conv, cards[cards.length - 1]]] : []);
  setFollowups([{ label: "Stop", intent: I("stop") }]);
  poll();
}
const runMeta = (job) => ({ env_label: job.env_label, provider: job.provider, running: job.running });

async function startRoam(a, conv = state.conv) {
  const { push, sayError, setFollowups } = inConv(conv);
  const overrides = pick(a, ["environment", "capsule_provider"]);
  const r = await api().start_roam(a.target, a.adapter || null, a.minutes || null,
    typeof a.memory === "boolean" ? a.memory : null, overrides);
  if (!r.ok) return sayError(r.error);
  const card = push({ role: "argus", kind: "roam", job: r.job.id, snap: r.job });
  if (!r.job.running) return onFinished([[conv, card]]);
  setFollowups([{ label: "Stop", intent: I("stop") }]);
  refreshInfo();
  poll();
}

function showDraft(draft, conv = state.conv) {
  const { push, sayError, setFollowups } = inConv(conv);
  if (!draft) return;
  if (!draft.ok && !draft.yaml) return sayError(draft.error || "The model didn't return a test.");
  push({ role: "argus", kind: "spec", draft });
  setFollowups(draft.ok
    ? [{ label: "Dry run it", intent: I("dry_run", { tests: "draft", draft: draft.id }) },
       { label: "Save it to .argus", intent: I("save_test", { draft: draft.id }) }]
    : [{ label: "Try again with more detail", prefill: "/write " }]);
}
function markDraftSaved(draftId) {
  if (!draftId) return;
  for (const c of [state.conv, ...state.conversations]) {
    for (const m of c.msgs) {
      if (m.kind === "spec" && m.draft && m.draft.id === draftId) update(m, { draft: Object.assign({}, m.draft, { saved: true }) });
    }
  }
}

async function knowledge(action, target, conv = state.conv) {
  const { push, say, sayError, setFollowups } = inConv(conv);
  if (action === "reset") {
    const k = await api().knowledge(target);
    if (!k.ok) return sayError(k.error);
    const name = k.target;
    if (!name) return sayError("Which target's knowledge should I reset?");
    if (!window.confirm(`Delete everything Argus learned about ${name}? This can't be undone.`)) {
      return say(`Kept the knowledge for ${name}.`);
    }
    const r = await api().knowledge_reset(name);
    if (!r.ok) return sayError(r.error);
    say(`Knowledge for ${name} is reset: 0 states, 0 transitions, 0 bug zones. The next roam starts from a blank graph.`);
    setFollowups([{ label: `Roam ${name} for 5 minutes`, intent: I("roam", { target: name, minutes: 5 }) }]);
    return;
  }
  if (action === "export") {
    const r = await api().knowledge_export(target || (state.info && state.info.last_target) || "");
    if (!r.ok) return sayError(r.error);
    say(`Exported the state graph for ${r.target} to ${r.path}.`);
    return;
  }
  const k = await api().knowledge(target);
  if (!k.ok) return sayError(k.error);
  push({ role: "argus", kind: "knowledge", k });
  setFollowups([
    { label: `Export knowledge for ${k.target}`, intent: I("knowledge", { action: "export", target: k.target }) },
    { label: `Roam ${k.target} for 5 minutes`, intent: I("roam", { target: k.target, minutes: 5 }) },
  ]);
}

async function switchProvider(name, conv = state.conv) {
  const { say, sayError, setFollowups } = inConv(conv);
  const r = await api().set_provider(name);
  if (!r.ok) return sayError(r.error);
  state.info = r;
  renderInfo();
  const budget = r.provider === "ollama" ? "Ollama runs locally, so only the time budget applies." : "The token budget applies too.";
  say(`Switched to ${r.provider} · ${r.model} for this session. .argus/config.yaml is unchanged. ${budget}`);
  setFollowups([{ label: "Check my provider connection and vision", intent: I("providers") }]);
}

/* ------------------------------------------------------------ polling --- */
// Jobs keep running when the user switches conversations, so polling covers
// every conversation, not just the open one.
function allConversations() {
  return [...new Set([state.conv, ...state.conversations])];
}
// A run card is active while its job runs, even when the card itself is already terminal
// (a spec error is final at once while the worker is still recording it).
const isActiveCard = (m) => (m.kind === "run" && (m.snap.status === "running" || m.snap.status === "queued" ||
  !!(m.meta && m.meta.running))) || (m.kind === "roam" && m.snap.running);
function activeJobIds() {
  const ids = new Set();
  for (const c of allConversations()) for (const m of c.msgs) if (isActiveCard(m)) ids.add(m.job);
  return ids;
}
function watchRunning() {
  return state.watchOn;
}
function poll() {
  if (state.closing) return;
  if (state.pollInFlight) { state.pollRequested = true; return; }
  if (state.pollTimer) return;
  state.pollTimer = setTimeout(tick, 150);
}
async function tick() {
  state.pollTimer = null;
  if (state.closing || state.pollInFlight) return;
  state.pollInFlight = true;
  state.pollDone = new Promise(resolve => state.finishPoll = resolve);
  try {
    const ids = activeJobIds();
    const finished = [];  // [conv, msg]
    for (const id of ids) {
      let job;
      try { job = await api().job_status(id); } catch (e) { continue; }
      if (!job.ok) {
        // A restarted backend cannot establish the outcome of an old job.
        for (const c of allConversations()) for (const m of c.msgs) if (m.job === id) markUnconfirmed(m);
        continue;
      }
      for (const c of allConversations()) {
        for (const m of c.msgs) {
          if (m.job !== id) continue;
          if (m.kind === "run") {
            const snap = job.runs[m.idx];
            const was = m.snap.status;
            const jobWasRunning = !!m.meta.running;
            if (JSON.stringify(snap) !== JSON.stringify(m.snap) || m.meta.running !== job.running) {
              update(m, { snap, meta: runMeta(job) });
            }
            if (((was === "running" || was === "queued") && !["running", "queued"].includes(snap.status)) ||
                (jobWasRunning && !job.running)) finished.push([c, m]);
          } else if (m.kind === "roam") {
            const was = m.snap.running;
            if (JSON.stringify(job) !== JSON.stringify(m.snap)) update(m, { snap: job });
            if (was && !job.running) finished.push([c, m]);
          }
        }
      }
    }
    if (state.watchOn || allConversations().some((c) => c.msgs.some((m) => m.kind === "watch" && m.watch.running))) {
      const w = await api().watch_status();
      state.watchOn = !!w.running;
      // Watched re-runs have no run card, so refresh the sidebar's test list and status
      // dots whenever another watched re-run finishes.
      const settled = w.settled_count ?? (w.events || []).filter((e) => !["running", "waiting"].includes(e.status)).length;
      if (settled !== state.watchSettled) {
        if (state.watchSettled !== null) refreshTests();
        state.watchSettled = settled;
      }
      applyWatchSnapshot(w);
    }
    await updateLive();
    if (finished.length) onFinished(finished);
    const busy = activeJobIds().size > 0 || watchRunning() || (state.liveJob && state.liveJob.running);
    renderBusy(busy);
  } catch (e) {
    // A transient bridge failure leaves active jobs eligible for the next tick.
  } finally {
    state.pollInFlight = false;
    state.finishPoll();
    state.pollDone = null;
    state.finishPoll = null;
    const again = state.pollRequested || activeJobIds().size > 0 || watchRunning() ||
      (state.liveJob && state.liveJob.running);
    state.pollRequested = false;
    if (!state.closing && again && !state.pollTimer) state.pollTimer = setTimeout(tick, 700);
  }
}

function applyWatchSnapshot(w) {
  let changed = false;
  for (const c of allConversations()) for (const m of c.msgs) {
    if (m.kind === "watch" && m.watch.id === w.id && JSON.stringify(w) !== JSON.stringify(m.watch)) {
      update(m, { watch: w });
      changed = true;
    }
  }
  if (changed) scheduleSave();
}

function onFinished(items) {
  refreshTests();
  refreshInfo();
  scheduleSave();
  // Follow-ups go to the conversation that owns each finished job, once the job is done.
  const done = new Map();  // job id -> [conv, last finished msg]
  for (const [c, m] of items) done.set(m.job, [c, m]);
  const stillActive = activeJobIds();
  for (const [jobId, [conv, last]] of done) {
    if (!stillActive.has(jobId)) followupsAfter(conv, last);
  }
}

// Only runs with an ATES run id have evidence; synthetic setup-error results do not.
const hasEvidence = (snap) => !!(snap && snap.key && snap.result && snap.result.ates_run_id);

function followupsAfter(conv, last) {
  const setFollowups = (l) => followupsIn(conv, l);
  if (last.kind === "run") {
    const runs = conv.msgs.filter((m) => m.kind === "run" && m.job === last.job);
    const failed = runs.find((m) => m.snap.status !== "pass");
    const explainable = failed && failed.snap.key && failed.snap.result;
    const lastRun = runs.length ? runs[runs.length - 1].snap : null;
    const lastKey = hasEvidence(lastRun) ? lastRun.key : null;
    setFollowups(failed
      ? [explainable ? { label: `Why did ${failed.snap.file} fail?`, intent: I("explain", { key: failed.snap.key }) } : null,
         { label: "Re-run in a Capsule and keep the Failure Capsule", intent: I("run", { tests: [failed.snap.file], environment: "capsule", retain: true }) },
         hasEvidence(failed.snap) ? { label: "Show ATES evidence", intent: I("evidence", { key: failed.snap.key }) } : null].filter(Boolean)
      : [lastKey ? { label: "Show ATES evidence", intent: I("evidence", { key: lastKey }) } : null,
         { label: "Run history", intent: I("report") },
         { label: "Run all tests", intent: I("run", { tests: "all" }) }].filter(Boolean));
  } else if (last.kind === "roam") {
    const s = last.snap;
    const list = [];
    if (s.regressions && s.regressions.length) list.push({ label: "Turn finding 1 into a test", stub: { job: s.id, index: 0 } });
    list.push({ label: `Show knowledge for ${s.target}`, intent: I("knowledge", { action: "show", target: s.target }) });
    list.push({ label: "Roam it again for 10 minutes", intent: I("roam", { target: s.target, adapter: s.adapter, minutes: 10 }) });
    setFollowups(list);
  }
}

async function regressionStub(job, index, conv = state.conv) {
  const draft = await api().regression_stub(job, index || 0);
  if (!draft.ok && !draft.yaml) return sayIn(conv, draft.error, { error: true });
  showDraft(draft, conv);
}

/* ---------------------------------------------------------- live panel --- */
async function updateLive() {
  let live;
  try { live = await api().live(); } catch (e) { return; }
  state.liveJob = live.has ? live : null;
  const has = !!live.has;
  $("live-empty").hidden = has;
  $("live-body").hidden = !has;
  $("live-badge").hidden = !has;
  if (!has) { $("live-sub").textContent = "Nothing running"; return; }
  $("live-sub").textContent = (live.kind === "roam" ? "Free roam" : "Test run") + " · " + live.title;
  const status = live.kind === "roam" && live.running ? "running" : live.status;
  const badge = $("live-badge");
  badge.className = "badge " + status;
  badge.textContent = live.kind === "roam" && live.running ? "Roaming" : (STATUS_LABEL[status] || status);
  $("live-action").textContent = live.action || "—";
  $("ls-states").textContent = live.states == null ? "—" : fmt(live.states);
  $("ls-transitions").textContent = live.transitions == null ? "—" : fmt(live.transitions);
  $("ls-bugs").textContent = live.bugs == null ? "—" : fmt(live.bugs);
  $("ls-progress").textContent = (live.progress || 0) + "%";
  $("live-where").textContent = adapterShort(live.adapter) + " · " + live.env_label;
  $("live-tokens").textContent = fmt(live.tokens) + " tokens";
  $("live-stop").hidden = !live.running;
  const screen = $("screen");
  const cli = live.adapter === "cli";
  screen.classList.toggle("cli", cli);
  if (live.id !== state.liveShown) {
    state.liveShown = live.id;
    state.liveTs = 0;
    $("screen-img").hidden = true;
    $("screen-ph").hidden = false;
  }
  if (cli) {
    $("screen-img").hidden = true;
    $("screen-ph").hidden = false;
    $("screen-ph").textContent = "$ " + (live.action || "");
  } else if (live.screenshot_ts > state.liveTs) {
    const shot = await api().capture_live(state.liveTs);
    if (shot.b64) {
      $("screen-img").src = "data:image/png;base64," + shot.b64;
      $("screen-img").hidden = false;
      $("screen-ph").hidden = true;
      state.liveTs = shot.ts;
    }
  } else if (!state.liveTs) {
    $("screen-ph").textContent = live.running ? "Waiting for the first screenshot…" : "No screenshot was captured.";
  }
}

/* ------------------------------------------------------------ renderers --- */
function renderThread() {
  const thread = $("thread");
  const conv = state.conv;
  const nodes = [];
  if (!conv.msgs.length) nodes.push(renderHero());
  for (const m of conv.msgs) {
    const cached = nodeCache.get(m.id);
    if (cached && cached.v === m.v) { nodes.push(cached.node); continue; }
    const node = renderMsg(m);
    nodeCache.set(m.id, { v: m.v, node });
    nodes.push(node);
  }
  thread.replaceChildren(...nodes);
  for (const log of thread.querySelectorAll(".roam-log")) log.scrollTop = log.scrollHeight;
}

function renderHero() {
  if (state.info && state.info.project_required) {
    return h("div", { class: "hero" },
      h("h1", { icon: "logoBig" }, "What should Argus test today?"),
      h("p", { class: "muted", text: state.info.error }),
      h("div", { class: "chips" }, h("button", { class: "chip hov", text: "Open project folder", onClick: openProject })));
  }
  const hasProject = !state.info || state.info.initialized;
  const starters = hasProject
    ? [
      { title: "Run all tests", fn: () => runChip("Run all tests", I("run", { tests: "all" })) },
      { title: "Free roam", fn: () => prefill("Roam ") },
      { title: "Write a test", fn: () => prefill("Write a test for ") },
      { title: "Check my model", fn: () => runChip("Check my provider connection and vision", I("providers")) },
    ]
    : [
      { title: "Set up this project", fn: () => runChip("Set up this project", I("init")) },
      { title: "Check my model", fn: () => runChip("Check my provider connection and vision", I("providers")) },
      { title: "What can Argus do?", fn: () => runChip("What can Argus do?", I("help")) },
    ];
  return h("div", { class: "hero" },
    h("h1", { icon: "logoBig" }, "What should Argus test today?"),
    h("div", { class: "chips" }, starters.map((s) => h("button", { class: "chip hov", onClick: s.fn, text: s.title }))));
}

function renderMsg(m) {
  if (m.role === "user") return h("div", { class: "msg-user" }, h("div", { text: m.text }));
  const wrap = h("div", { class: "msg-argus" });
  const R = RENDER[m.kind];
  wrap.appendChild(R ? R(m) : h("div", { class: "say", text: "" }));
  return wrap;
}

const RENDER = {
  thinking: () => h("div", { class: "thinking", "aria-label": "Argus is working", icon: "logo" }, "Working…"),
  text: (m) => h("div", { class: "say" + (m.error ? " err" : ""), text: m.text }),
  run: renderRun,
  roam: renderRoam,
  spec: renderSpec,
  dry: (m) => h("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } },
    h("div", { class: "say", style: { fontSize: "16px" }, text: "Here's what would run. Nothing was executed." }),
    m.items.map((it) => h("div", { class: "dry-item" },
      h("div", { class: "top" }, h("b", { text: it.file, style: { fontWeight: 500 } }),
        h("span", { text: it.error ? "spec error" : `${adapterShort(it.adapter)} · ${it.steps.length} steps` })),
      it.error ? h("div", { class: "st", style: { color: "var(--fail)" }, text: it.error }) : null,
      it.steps.map((st, i) => h("div", { class: "st" }, `${i + 1}. `, st.kind === "assert" ? h("i", { text: "assert · " }) : null, st.text))))),
  providers: renderProviders,
  tokens: (m) => {
    const u = m.usage;
    const note = u.provider === "ollama" ? "Ollama runs locally: no per-token cost, time budget only."
      : `Token budget: ${u.max_tokens ? fmt(u.max_tokens) : "none"} (budgets.max_tokens in .argus/config.yaml).`;
    return h("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } },
      h("div", { class: "stat-grid" },
        h("div", {}, h("div", { class: "big", text: fmt(u.session.total_tokens) }), h("div", { class: "lbl", text: "this session" })),
        h("div", {}, h("div", { class: "big", text: fmt(u.project.total_tokens) }), h("div", { class: "lbl", text: "project total" })),
        h("div", {}, h("div", { class: "big", text: fmt(u.project.calls) }), h("div", { class: "lbl", text: "model calls" }))),
      h("div", { class: "lbl", style: { fontSize: "13px" }, text: note }));
  },
  report: (m) => h("div", { class: "list-card" },
    h("div", { class: "table-row th" }, h("span", { text: "Test" }), h("span", { text: "Status" }), h("span", { text: "Steps" }), h("span", { text: "Time" }), h("span", { class: "r", text: "Tokens" })),
    m.rows.map((r) => h("div", { class: "table-row" },
      h("span", { text: r.test, title: r.provider }),
      h("span", { class: "st-" + (["pass", "fail", "error"].includes(r.status) ? r.status : "other"), text: STATUS_LABEL[r.status] || r.status }),
      h("span", { class: "muted", text: r.steps }), h("span", { class: "muted", text: r.duration + "s" }),
      h("span", { class: "muted r", text: fmt(r.tokens) })))),
  knowledge: (m) => {
    const k = m.k;
    return h("div", { class: "list-card" }, h("div", { class: "know" },
      h("div", { style: { fontSize: "14px", fontWeight: 500 }, text: `What Argus knows about ${k.target}` }),
      h("div", { class: "grid4" },
        h("div", {}, h("div", { class: "big", text: fmt(k.states) }), h("div", { class: "lbl", text: "UI states" })),
        h("div", {}, h("div", { class: "big", text: fmt(k.transitions) }), h("div", { class: "lbl", text: "transitions" })),
        h("div", {}, h("div", { class: "big red", text: fmt(k.bugs) }), h("div", { class: "lbl", text: "bug zones" })),
        h("div", {}, h("div", { class: "big", text: fmt(k.sessions) }), h("div", { class: "lbl", text: "sessions" }))),
      h("div", { class: "actions" },
        h("span", { class: "grow lbl", text: k.backend }),
        h("button", { class: "btn danger hov", text: "Reset", onClick: () => runChip(`Reset knowledge for ${k.target}`, I("knowledge", { action: "reset", target: k.target })) }),
        h("button", { class: "btn dark", text: "Export", onClick: () => runChip(`Export knowledge for ${k.target}`, I("knowledge", { action: "export", target: k.target })) }))));
  },
  evidence: (m) => {
    const e = m.e;
    return h("div", { class: "list-card" },
      h("div", { class: "ev-head", icon: e.verified ? "shieldOk" : "shieldBad" },
        h("span", { class: "txt" },
          h("b", { class: e.verified ? "ok" : "bad", text: e.headline }),
          h("span", { class: "path", text: `${e.title} · ${e.path}` }))),
      e.rows.map((r) => h("div", { class: "kv" }, h("span", { class: "k", text: r.k }), h("span", { class: "v", text: r.v }))),
      h("div", { class: "note", text: e.verified ? e.note : (e.detail || "") }));
  },
  env: (m) => h("div", { class: "list-card" },
    h("div", { class: "head", text: `Where Argus runs · ${m.env.label}` }),
    m.env.rows.map((r) => h("div", { class: "kv" }, h("span", { class: "k", text: r.k }), h("span", { class: "v", text: r.v }))),
    h("div", { class: "note", text: m.env.note })),
  init: (m) => h("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } },
    h("div", { class: "say", text: m.files.some((f) => f.created) ? "Your project is set up. I created:" : "This project was already set up:" }),
    h("div", { class: "list-card" }, m.files.map((f) => h("div", { class: "kv" },
      h("span", { class: "mono", style: { fontSize: "12px" }, text: f.path }),
      h("span", { class: "k", text: f.created ? f.note : "already there" }))))),
  watch: renderWatch,
  help: (m) => h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
    h("div", { class: "say", text: "Ask in your own words, or use a command. Everything Argus does is available here:" }),
    h("div", { class: "help-grid" }, m.groups.map((g) => h("div", { class: "help-group" },
      h("div", { class: "gt", text: g.title }),
      g.items.map((it) => h("button", { class: "help-item hov",
        onClick: () => (it.text ? sendText(it.text) : prefill(it.cmd.replace(" …", " "))) },
        h("span", { class: "c", text: it.cmd }), h("span", { class: "d", text: it.desc }))))))),
};

function stepGlyph(state) {
  return iconEl({ pass: "pass", fail: "fail", error: "fail", active: "active", pending: "pending", skipped: "skipped" }[state] || "pending", "step-glyph");
}

function renderRun(m) {
  const r = m.snap;
  const res = r.result || {};
  const done = !["running", "queued"].includes(r.status);
  const planned = r.planned || [];
  const rows = [];
  const total = Math.max(planned.length, r.steps.length);
  for (let i = 0; i < total; i++) {
    const sr = r.steps[i];
    let st, text;
    if (sr) {
      st = sr.status === "pass" ? "pass" : sr.status === "skipped" ? "skipped" : "fail";
      text = sr.text;
    } else {
      st = r.status === "running" && i === r.steps.length ? "active" : done ? "skipped" : "pending";
      text = planned[i].text;
    }
    const failed = sr && (sr.status === "fail" || sr.status === "error");
    rows.push(h("div", { class: "step" },
      h("div", { class: "step-row" }, stepGlyph(st), h("span", { class: "step-text", text }),
        h("span", { class: "step-dur", text: sr && sr.duration_s ? sr.duration_s.toFixed(2) + "s" : "" })),
      sr && sr.actions && sr.actions.length ? h("div", { class: "step-subs" }, sr.actions.map((a) => h("div", { text: "↳ " + a }))) : null,
      failed ? h("div", { class: "step-fail" },
        sr.expected ? h("div", { class: "exp", text: "expected  " + sr.expected }) : null,
        sr.actual ? h("div", { class: "act", text: "actual    " + sr.actual }) : null,
        sr.note ? h("div", { class: "note", text: sr.note }) : null) : null,
      sr && sr.status === "skipped" && sr.note ? h("div", { class: "step-subs" }, h("div", { text: sr.note })) : null,
      sr && sr.flaky ? h("div", { class: "step-subs" }, h("div", { text: "passed on retry" })) : null));
  }
  const steps = res.steps || r.steps;
  const passed = steps.filter((s) => s.status === "pass").length;
  const failedN = steps.filter((s) => s.status === "fail" || s.status === "error").length;
  const skipped = steps.filter((s) => s.status === "skipped").length + Math.max(0, planned.length - steps.length);
  const tokens = (res.tokens || {}).total_tokens || 0;
  const exit = r.status === "pass" ? 0 : r.status === "error" ? 2 : 1;
  const summary = `${passed} passed · ${failedN} failed · ${skipped} skipped · ${(res.duration_s || 0).toFixed(1)}s · ${fmt(tokens)} tokens · exit ${exit}`;
  const progress = planned.length ? Math.round(100 * Math.min(r.steps.length, planned.length) / planned.length) : 0;
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, iconEl("flask", "icon-tile"),
      h("span", { class: "head-text" }, h("b", { text: r.file }),
        h("span", { text: `${adapterShort(r.adapter)} · ${m.meta.env_label} · ${m.meta.provider}` })),
      h("span", { class: "badge " + r.status, text: STATUS_LABEL[r.status] || r.status })),
    r.status === "running" ? h("div", { class: "progress" }, h("div", { style: { width: progress + "%" } })) : null,
    rows.length ? h("div", { class: "steps" }, rows) : null,
    h("div", { class: "card-foot" },
      (r.notes || []).map((n) => h("div", { class: "line", text: n })),
      done ? h("div", { class: "actions", style: { marginTop: "6px" } },
        h("span", { class: "grow summary", text: r.result ? summary : "" }),
        hasEvidence(r) ? h("button", { class: "btn hov", text: "Evidence", onClick: () => runChip("Show ATES evidence", I("evidence", { key: r.key })) }) : null,
        h("button", { class: "btn dark", text: "Run again", onClick: () => runChip(`Re-run ${r.file}`, I("run", { tests: [r.file] })) })) : null));
}

function renderRoam(m) {
  const s = m.snap;
  const running = s.running;
  const now = running ? Date.now() / 1000 : (s.ended_at || Date.now() / 1000);
  const elapsed = `${mmss(now - s.started_at)} / ${mmss(s.minutes * 60)}`;
  const status = running ? "running" : s.status;
  const liveFindings = s.log.filter((l) => l.startsWith("FINDING")).length;
  const findings = s.findings || [];
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, iconEl("compass", "icon-tile"),
      h("span", { class: "head-text" }, h("b", { text: (running ? "Roaming " : "Roamed ") + s.target }),
        h("span", { text: `${adapterShort(s.adapter)} · ${s.env_label} · Memory ${s.memory ? "on" : "off"} · ${elapsed}` })),
      h("span", { class: "badge " + status, text: running ? "Roaming" : STATUS_LABEL[status] || status })),
    h("div", { class: "roam-log scroll" }, s.log.slice(-200).map((l) => {
      const f = /^FINDING \[(\w+)\]/.exec(l);
      return h("div", { class: f ? f[1].toLowerCase() : "", text: l });
    })),
    h("div", { class: "roam-body" },
      findings.map((f) => h("div", { class: "finding" },
        h("span", { class: "sev " + String(f.severity).toLowerCase(), text: f.severity }),
        h("span", {}, h("b", { text: f.title }), h("span", { class: "d", text: `Expected ${f.expected}. Got: ${f.actual}.` })))),
      h("div", { class: "actions" },
        h("span", { class: "grow lbl", text: `${running ? liveFindings : findings.length} findings · ${s.log.length} events · ${fmt(s.tokens)} tokens` }),
        running ? h("button", { class: "btn danger hov", text: "Stop", onClick: () => execute(I("stop")) }) : null,
        !running ? h("button", { class: "btn hov", text: "Knowledge", onClick: () => runChip(`Show knowledge for ${s.target}`, I("knowledge", { action: "show", target: s.target })) }) : null,
        !running && s.regressions && s.regressions.length ? h("button", { class: "btn dark", text: "Write regression test", onClick: () => { push({ role: "user", kind: "user", text: "Turn finding 1 into a test" }); regressionStub(s.id, 0); } }) : null),
      !running && s.report ? h("div", { class: "summary", text: s.report + (s.stopped_reason ? ` · ${s.stopped_reason}` : "") }) : null));
}

function renderSpec(m) {
  const d = m.draft;
  return h("div", { class: "card" },
    h("div", { class: "card-head tight", icon: "fileDark" },
      h("span", { style: { flexGrow: 1, fontSize: "14px", fontWeight: 500 }, text: d.file || "draft.test.yaml" }),
      h("span", { class: "lbl", text: d.adapter ? `YAML · ${d.adapter}` : "YAML" })),
    h("pre", { class: "yaml", text: d.yaml }),
    h("div", { class: "card-bar" },
      d.ok ? h("span", { class: "grow", text: d.saved ? `Saved to .argus/${d.file}` : (d.from_finding ? "From a roam finding · refine before committing" : "Review it, then save it to .argus/") })
        : h("span", { class: "grow bad", text: "Not valid yet: " + (d.error || "unknown error") }),
      d.ok ? h("button", { class: "btn hov", text: "Dry run", onClick: () => runChip("Dry run it", I("dry_run", { tests: "draft", draft: d.id })) }) : null,
      d.ok ? h("button", { class: "btn dark", text: d.saved ? "Saved" : "Save to .argus", disabled: d.saved, onClick: () => runChip("Save it to .argus", I("save_test", { draft: d.id })) }) : null));
}

function renderProviders(m) {
  const s = m.status;
  let lead;
  if (s.ok) {
    lead = `${s.detail}. ` + (s.vision === false ? "This model can't see screenshots, so Argus will use the accessibility tree only." : "Vision supported: screenshots + accessibility tree.");
  } else {
    lead = `I couldn't use ${s.provider} · ${s.model}: ${s.detail}`;
  }
  return h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
    h("div", { class: "say" + (s.ok ? "" : " err"), text: lead }),
    h("div", { class: "list-card" }, (s.providers || []).map((p) => h("button", {
      class: "prov hov", role: "menuitemradio", "aria-checked": p.current ? "true" : "false",
      onClick: () => p.current ? null : runChip(`Switch to ${p.type}`, I("switch_provider", { provider: p.type })) },
      h("span", { class: "radio" }, p.current ? h("i") : null),
      h("span", { class: "t", text: p.type }), h("span", { class: "m", text: p.model }), h("span", { class: "n", text: p.note })))));
}

function renderWatch(m) {
  const w = m.watch;
  return h("div", { class: "list-card" }, h("div", { class: "watch" },
    h("div", { class: "top" }, h("b", { text: `Watching ${w.pattern || ".argus/*.test.yaml"}` }),
      h("span", { class: "badge " + (w.running ? "running" : "stopped"), text: w.running ? "Watching" : "Stopped" }),
      w.running ? h("button", { class: "btn hov", style: { height: "28px" }, text: "Stop", onClick: () => execute(I("watch", { action: "stop" })) }) : null),
    (w.events || []).length ? w.events.slice(-12).map((e) => h("div", { class: "ev" },
      h("span", { class: "at", text: e.at }), h("span", { class: "t", text: `${e.file} ${e.change === "removed" ? "removed" : "changed"}` }),
      h("span", { class: "st-" + (["pass", "fail", "error"].includes(e.status) ? e.status : "other"), text: e.summary })))
      : h("div", { class: "lbl", text: "Edit any .test.yaml in .argus/ and Argus re-runs it here." })));
}

function renderFollowups() {
  const box = $("followups");
  box.replaceChildren(...(state.conv.followups || []).map((f) => h("button", { class: "btn hov", text: f.label, onClick: () => followup(f) })));
}
function followup(f) {
  if (f.prefill) return prefill(f.prefill);
  if (f.stub) { push({ role: "user", kind: "user", text: f.label }); setFollowups([]); return regressionStub(f.stub.job, f.stub.index); }
  if (f.intent && f.intent.intent === "stop") return execute(f.intent);
  return runChip(f.label, f.intent);
}
function prefill(text) {
  const input = $("input");
  input.value = text;
  input.focus();
  input.setSelectionRange(text.length, text.length);
}

function renderBusy(busy) {
  $("stop-btn").hidden = !busy;
  $("send-btn").hidden = busy;
}

/* --------------------------------------------------------- sidebar & info --- */
async function refreshInfo() {
  try { state.info = await api().app_info(); } catch (e) {
    sayIn(state.conv, "Could not load project settings. Use Open project folder to try another project.", { error: true });
    return;
  }
  renderInfo();
  if (state.info.ok === false && !state.info.project_required && state.configError !== state.info.error) {
    state.configError = state.info.error;
    sayIn(state.conv, state.info.error, { error: true });
    followupsIn(state.conv, [{ label: "Retry settings", intent: I("retry_settings") }]);
  } else if (state.info.ok !== false) state.configError = null;
  if (!state.conv.msgs.length) renderThread();
}
function renderInfo() {
  const info = state.info;
  if (!info) return;
  $("project-name").textContent = info.project_name;
  $("project-name").title = info.project;
  $("project-meta").textContent = `${fmt(info.tokens.total_tokens)} tokens · Argus v${info.version}`;
  $("project-meta").title = `Argus v${info.version}`;
  $("env-label").textContent = info.env_label;
  $("model-label").textContent = info.model;
  $("model-btn").title = `${info.provider} · ${info.model}`;
  $("memory-state").textContent = info.memory ? "on" : "off";
  $("memory-state").className = info.memory ? "on" : "";
  $("memory-btn").setAttribute("aria-pressed", info.memory ? "true" : "false");
  $("retain-btn").setAttribute("aria-checked", info.retain ? "true" : "false");
  $("open-project").disabled = info.can_open_project === false;
}

async function refreshTests() {
  try { state.tests = await api().list_tests(); } catch (e) { state.tests = []; }
  const list = $("test-list");
  if (!state.tests.length) {
    list.replaceChildren(h("div", { class: "side-empty", text: state.info && state.info.project_required ? "Open a project folder to see its tests." : (state.info && state.info.initialized ? "No .test.yaml files yet." : "No .argus/ here yet. Ask Argus to set up this project.") }));
    return;
  }
  const color = { pass: "var(--pass)", fail: "var(--fail)", error: "var(--error)" };
  list.replaceChildren(...state.tests.map((t) => h("button", {
    class: "test-item hov", title: t.error ? t.error : `${t.name} · ${t.steps} steps · ${adapterShort(t.adapter)} — click to run`,
    onClick: () => runChip(`Run ${t.file}`, I("run", { tests: [t.file] })) },
    iconEl("file"), h("span", { class: "name", text: t.file }),
    h("span", { class: "dot", style: { background: t.error ? "var(--error)" : (color[t.last] || "var(--ink-7)") } }))));
}

function renderRecents() {
  const list = $("recents");
  const convs = [state.conv, ...state.conversations.filter((c) => c.id !== state.conv.id)]
    .filter((c) => c.msgs.length || c === state.conv).slice(0, 12);
  const items = convs.filter((c) => c.title).map((c) => h("button", {
    class: "recent hov" + (c.id === state.conv.id ? " current" : ""), text: c.title, title: c.title,
    onClick: () => openConversation(c.id) }));
  list.replaceChildren(...(items.length ? items : [h("div", { class: "side-empty", text: "Your conversations appear here." })]));
}

function openConversation(id) {
  if (id === state.conv.id) return;
  stash();
  const c = state.conversations.find((x) => x.id === id);
  if (!c) return;
  state.conv = c;
  $("chat-title").textContent = c.title || "Chat with Argus";
  renderAll();
  poll();
}
function newChat() {
  stash();
  state.conv = newConversation();
  $("chat-title").textContent = "Chat with Argus";
  renderAll();
  $("input").focus();
}
function stash() {
  const c = state.conv;
  if (!c || !c.msgs.length) return;
  state.conversations = [c, ...state.conversations.filter((x) => x.id !== c.id)];
  scheduleSave();
}
function scheduleSave() {
  clearTimeout(state.saveTimer);
  if (state.closing) return;
  state.saveTimer = setTimeout(() => flushConversations(), 400);
}

function flushConversations() {
  clearTimeout(state.saveTimer);
  const c = state.conv;
  if (c && c.msgs.length) state.conversations = [c, ...state.conversations.filter((x) => x.id !== c.id)];
  const clean = JSON.parse(JSON.stringify(state.conversations.slice(0, 30).map((conv) => Object.assign({}, conv, {
    msgs: conv.msgs.filter((m) => m.kind !== "thinking"),
  }))));
  const save = state.saveChain.then(async () => {
    try {
      const r = await api().save_conversations(clean);
      if (!r || r.ok !== true) throw new Error("Save not confirmed");
      state.saveError = false;
      return { ok: true };
    } catch (e) {
      if (!state.saveError) {
        state.saveError = true;
        sayIn(state.conv, "Could not save this conversation. Check your user-data folder permissions before closing Argus.", { error: true });
      }
      return { ok: false };
    }
  });
  state.saveChain = save;
  renderRecents();
  return save;
}

function resumeAfterCloseFailure() {
  state.closing = false;
  for (const el of state.closeControls || []) el.disabled = false;
  state.closeControls = [];
  sayIn(state.conv, "This window stayed open because its latest conversation could not be saved. Wait for any response, check your user-data folder permissions, then try closing again.", { error: true });
  poll();
}

async function flushConversationsForClose() {
  if (!state.conv || state.pendingActions || allConversations().some((c) => c.msgs.some((m) => m.kind === "thinking"))) return { ok: false };
  state.closing = true;
  clearTimeout(state.saveTimer);
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.closeControls = [...document.querySelectorAll("button, input, textarea")].filter((el) => !el.disabled);
  for (const el of state.closeControls) el.disabled = true;
  if (state.info && state.info.project_required) return { ok: true };
  try {
    if (state.pollDone) await state.pollDone;
    const watch = await api().watch_status();
    state.watchOn = !!watch.running;
    applyWatchSnapshot(watch);
    const result = await flushConversations();
    return result;
  } catch (e) { return { ok: false }; }
}
function renderAll() {
  renderThread();
  renderFollowups();
  renderRecents();
  $("chat-title").textContent = state.conv.title || "Chat with Argus";
}

/* ---------------------------------------------------------------- menus --- */
function toggleMenu(name) {
  state.menu = state.menu === name ? null : name;
  for (const n of ["tools", "env", "model"]) {
    $(n + "-menu").hidden = state.menu !== n;
    $(n + "-btn").setAttribute("aria-expanded", state.menu === n ? "true" : "false");
  }
  $("scrim").hidden = !state.menu;
  if (state.menu) {
    buildMenu(state.menu);
    $(name + "-menu").querySelector("button:not(:disabled)")?.focus();
  }
}
const closeMenu = () => { if (state.menu) toggleMenu(state.menu); };

function menuItem(label, hint, fn, extra) {
  return h("button", { class: "menu-item hov", role: extra && extra.radio ? "menuitemradio" : "menuitem",
    "aria-checked": extra && extra.radio ? (extra.current ? "true" : "false") : null,
    onClick: async () => {
      closeMenu();
      try { await fn(); } catch (e) { sayIn(state.conv, String(e), { error: true }); }
    } },
    extra && extra.desc ? h("span", { class: "txt" }, h("b", { text: label }), h("span", { text: extra.desc })) : h("span", { text: label }),
    extra && extra.radio ? (extra.current ? iconEl("check") : null) : h("span", { class: "h", text: hint }));
}

function buildMenu(name) {
  const info = state.info || {};
  const target = info.last_target;
  let items = [];
  if (name === "tools") {
    items = [
      menuItem("Run all tests", "/run", () => runChip("Run all tests", I("run", { tests: "all" }))),
      menuItem("Dry run", "/dry-run", () => runChip("Dry run all", I("dry_run", { tests: "all" }))),
      menuItem("Watch for changes", "/watch", () => runChip("Watch for changes", I("watch", { action: "start" }))),
      menuItem("Free roam", "/roam", () => target ? runChip(`Roam ${target} for 5 minutes`, I("roam", { target, minutes: 5 })) : prefill("/roam ")),
      menuItem("Write a test", "plain English", () => prefill("Write a test for ")),
      menuItem("Knowledge graph", "/knowledge", () => runChip("Show knowledge", I("knowledge", { action: "show" }))),
      menuItem("ATES evidence", "/evidence", () => runChip("Show ATES evidence", I("evidence"))),
      menuItem("Run history", "/report", () => runChip("Run history", I("report"))),
      menuItem("Token usage", "/tokens", () => runChip("Token usage", I("tokens"))),
      menuItem("Execution environment", "/env", () => runChip("Execution environment", I("environment"))),
      menuItem("Initialize project", "/init", () => runChip("Initialize project", I("init"))),
    ];
  } else if (name === "env") {
    const env = info.environment, cap = info.capsule_provider;
    const opts = [
      ["Local", "Shared host · fastest", "local", null],
      ["Capsule · auto", "Disposable VM for this host", "capsule", "auto"],
      ["Capsule · Hyper-V", "Windows guest, Gen-2 VM", "capsule", "hyperv"],
      ["Capsule · libvirt/KVM", "Linux guest, qcow2 overlay", "capsule", "libvirt"],
    ];
    items = opts.map(([label, desc, e, c]) => menuItem(label, "", async () => {
      const r = await api().set_environment(e, c);
      if (r.ok) { state.info = r; renderInfo(); } else sayIn(state.conv, r.error, { error: true });
    }, { desc, radio: true, current: env === e && (e === "local" || cap === c) }));
  } else if (name === "model") {
    items = (info.providers || []).map((p) => menuItem(`${p.type} · ${p.model}`, "", () => {
      if (!p.current) return switchProvider(p.type);
    }, { desc: p.note, radio: true, current: p.current }));
    if (!items.length) items = [h("div", { class: "side-empty", text: "No providers configured. Run /init first." })];
  }
  $(name + "-menu").replaceChildren(...items);
}

/* ------------------------------------------------------------------ boot --- */
function wire() {
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("input");
    const text = input.value;
    input.value = "";
    sendText(text);
  });
  $("new-chat").addEventListener("click", newChat);
  $("open-project").addEventListener("click", openProject);
  $("help-btn").addEventListener("click", () => runChip("What can Argus do?", I("help")));
  for (const b of document.querySelectorAll("[data-nav]")) {
    b.addEventListener("click", () => {
      const nav = b.dataset.nav;
      const target = state.info && state.info.last_target;
      if (nav === "tests") runChip("Dry run all", I("dry_run", { tests: "all" }));
      else if (nav === "roam") target ? runChip(`Roam ${target} for 5 minutes`, I("roam", { target, minutes: 5 })) : prefill("/roam ");
      else if (nav === "knowledge") runChip("Show knowledge", I("knowledge", { action: "show" }));
      else if (nav === "evidence") runChip("Show ATES evidence", I("evidence"));
    });
  }
  $("tools-btn").addEventListener("click", () => toggleMenu("tools"));
  $("env-btn").addEventListener("click", () => toggleMenu("env"));
  $("model-btn").addEventListener("click", () => toggleMenu("model"));
  $("scrim").addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => {
    if (!state.menu) return;
    const name = state.menu;
    if (e.key === "Escape") { closeMenu(); $(name + "-btn").focus(); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const items = Array.from($(name + "-menu").querySelectorAll("button:not(:disabled)"));
    if (!items.length) return;
    let next = items.indexOf(document.activeElement) + (e.key === "ArrowUp" ? -1 : 1);
    if (e.key === "Home") next = 0;
    if (e.key === "End") next = items.length - 1;
    items[(next + items.length) % items.length].focus();
  });
  $("memory-btn").addEventListener("click", async () => {
    try {
      const r = await api().set_memory(!(state.info && state.info.memory));
      state.info = r; renderInfo();
    } catch (e) { sayIn(state.conv, String(e), { error: true }); }
  });
  $("retain-btn").addEventListener("click", async () => {
    try {
      const r = await api().set_retain(!(state.info && state.info.retain));
      state.info = r; renderInfo();
    } catch (e) { sayIn(state.conv, String(e), { error: true }); }
  });
  const stop = () => execute(I("stop"));
  $("stop-btn").addEventListener("click", stop);
  $("live-stop").addEventListener("click", stop);
  window.addEventListener("arguscloseblocked", () => {
    sayIn(state.conv, "Stopping the active job before closing. Wait for its result and cleanup, then close this window again.");
    poll();
  });
  window.addEventListener("argusclosesavefailed", resumeAfterCloseFailure);
}

async function openProject() {
  if (state.projectOpening) return;
  state.projectOpening = true;
  const button = $("open-project");
  button.disabled = true;
  try {
    const result = await api().open_project();
    if (!result.ok) sayIn(state.conv, result.error, { error: true });
  } catch (e) { sayIn(state.conv, "Could not open the folder chooser. Try reopening the desktop app.", { error: true }); }
  finally { button.disabled = false; state.projectOpening = false; }
}

function markUnconfirmed(m) {
  if (m.kind === "run") {
    m.snap.status = "unknown";
    m.meta.running = false;
    m.snap.notes = [...(m.snap.notes || []), "Completion could not be confirmed. Check run history and evidence before retrying."];
  } else if (m.kind === "roam") {
    m.snap.running = false;
    m.snap.status = "unknown";
  }
  m.v = (m.v || 0) + 1;
}

function restoreConversations(saved) {
  if (!Array.isArray(saved)) return [];
  return saved.filter((c) => c && typeof c.id === "string" && Array.isArray(c.msgs)).slice(0, 30).map((c) => {
    const msgs = c.msgs.filter((m) => m && typeof m.id === "string" && m.kind !== "thinking").map((m) => {
      try { renderMsg(m); return m; }
      catch (e) {
        return { id: m.id, role: "argus", kind: "text", error: true,
          text: "This saved result could not be restored. Check run history and evidence for the original result." };
      }
    });
    return Object.assign({}, c, { msgs, title: typeof c.title === "string" ? c.title : "Restored chat",
      followups: [], seq: Math.max(Number(c.seq) || 1, msgs.length + 1) });
  });
}

async function restoreJobs() {
  const finished = [];
  for (const id of activeJobIds()) {
    let job;
    try { job = await api().job_status(id); } catch (e) { job = { ok: false }; }
    for (const c of allConversations()) for (const m of c.msgs) {
      if (m.job !== id) continue;
      if (job.ok && m.kind === "run" && job.runs?.[m.idx]) {
        m.snap = job.runs[m.idx]; m.meta = runMeta(job);
        if (!job.running) finished.push([c, m]);
      } else if (job.ok && m.kind === "roam") m.snap = job;
      else markUnconfirmed(m);
      if (job.ok && m.kind === "roam" && !job.running) finished.push([c, m]);
    }
  }
  let watch;
  try { watch = await api().watch_status(); } catch (e) { watch = { running: false }; }
  state.watchOn = !!watch.running;
  for (const c of allConversations()) for (const m of c.msgs) if (m.kind === "watch") {
    m.watch = watch.running && watch.id === m.watch.id ? watch : Object.assign({}, m.watch, { running: false });
  }
  if (finished.length) onFinished(finished);
}

async function boot() {
  if (window._booted) return;
  window._booted = true;
  wire();
  state.conv = newConversation();
  renderAll();
  await refreshInfo();
  await refreshTests();
  try {
    const saved = await api().load_conversations();
    state.conversations = restoreConversations(saved);
  } catch (e) { state.conversations = []; }
  await restoreJobs();
  renderAll();
  await updateLive();
  poll();
  $("input").focus();
}

window.addEventListener("pywebviewready", boot);
// Some pywebview versions fire ready before our listener attaches.
setTimeout(() => { if (window.pywebview && window.pywebview.api && !window._booted) boot(); }, 600);
