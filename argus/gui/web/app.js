/* Argus desktop app — UI logic. Talks to Python via window.pywebview.api. */

let selectedTest = null;
let polling = null;
let liveViewActive = false;
let liveViewInterval = null;
let liveStatsInterval = null;

const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

/* ---- boot ------------------------------------------------------------ */

window.addEventListener("pywebviewready", boot);
// Fallback: some pywebview versions fire ready before our listener attaches.
setTimeout(() => { if (window.pywebview && !window._booted) boot(); }, 600);

async function boot() {
  if (window._booted) return;
  window._booted = true;
  const info = await api().app_info();
  $("proj").textContent = info.project;
  $("sb-provider").textContent = `provider: ${info.provider}:${info.model}`;
  $("sb-version").textContent = `v${info.version}`;
  $("p-type").textContent = info.provider;
  $("p-model").textContent = info.model;
  $("p-time").textContent = info.time_minutes ? `${info.time_minutes} min` : "none";
  $("p-tokens").textContent =
    info.provider === "ollama" ? "n/a (local)" : (info.max_tokens || "none");
  if (navigator.userAgent.includes("Windows")) $("roam-warn").style.display = "block";
  await refreshTests();
  await refreshTokens();
  setInterval(refreshTokens, 5000);
}

/* ---- view nav --------------------------------------------------------- */

document.querySelectorAll(".viewnav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".viewnav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".view").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`view-${b.dataset.view}`).classList.add("active");
  })
);

/* ---- tests ------------------------------------------------------------- */

async function refreshTests() {
  const tests = await api().list_tests();
  const list = $("test-list");
  if (!tests.length) {
    list.innerHTML = `<div class="empty">No tests yet —<br>click <b>init</b> above.</div>`;
    return;
  }
  list.innerHTML = "";
  tests.forEach((t) => {
    const btn = document.createElement("button");
    btn.className = "test-item" + (t.file === selectedTest ? " active" : "");
    btn.innerHTML = `<span class="name">${esc(t.file)}</span>
      <span class="meta">${t.error ? "⚠" : t.steps + " steps"}</span>`;
    btn.onclick = () => selectTest(t.file, btn);
    list.appendChild(btn);
  });
}

function selectTest(file, btn) {
  selectedTest = file;
  document.querySelectorAll(".test-item").forEach((x) => x.classList.remove("active"));
  btn.classList.add("active");
  $("run-title").textContent = file;
  $("run-btn").disabled = false;
  $("run-badge").innerHTML = "";
  $("run-body").innerHTML = `<div class="empty">Ready — press Run.</div>`;
}

async function initProject() {
  await api().init_project();
  await refreshTests();
}

/* ---- run ----------------------------------------------------------------- */

async function runSelected() {
  if (!selectedTest) return;
  const res = await api().run_test(selectedTest);
  if (!res.ok) { alert(res.error); return; }
  $("run-btn").disabled = true;
  $("run-badge").innerHTML = badge("running", "Running");
  $("run-body").innerHTML = "";
  $("sb-state").textContent = `running ${selectedTest}…`;
  clearInterval(polling);
  polling = setInterval(pollRun, 700);
  startLiveStats();
}

async function pollRun() {
  const st = await api().run_status();
  renderSteps(st.steps);
  if (!st.running) {
    clearInterval(polling);
    $("run-btn").disabled = false;
    $("sb-state").textContent = "idle";
    const r = st.result || {};
    const status = r.status || "error";
    $("run-badge").innerHTML = badge(status, status);
    if (r.error) {
      $("run-body").insertAdjacentHTML("beforeend",
        `<div class="step fail"><span class="glyph error">✗</span>
         <span class="text">${esc(r.error)}</span></div>`);
    }
  }
}

function renderSteps(steps) {
  const body = $("run-body");
  body.innerHTML = "";
  (steps || []).forEach((s) => {
    const glyphs = { pass: "✓", fail: "✗", error: "✗", skipped: "⊘" };
    let sub = "";
    (s.actions || []).forEach((a) => { sub += `<span class="sub">↳ ${esc(a)}</span>`; });
    if (s.status === "fail" && s.expected)
      sub += `<div class="diff"><div class="exp">exp  ${esc(s.expected)}</div>
              <div class="act">act  ${esc(s.actual || "")}</div></div>`;
    else if (s.note && s.status !== "pass") sub += `<span class="sub">${esc(s.note)}</span>`;
    body.insertAdjacentHTML("beforeend",
      `<div class="step ${s.status === "fail" ? "fail" : ""}">
         <span class="glyph ${s.status}">${glyphs[s.status] || "·"}</span>
         <span class="text">${esc(s.text)}${sub}</span>
         <span class="dur">${s.duration_s ? s.duration_s.toFixed(2) + "s" : ""}</span>
       </div>`);
  });
  body.scrollTop = body.scrollHeight;
}

/* ---- roam ------------------------------------------------------------------ */

async function startRoam() {
  const target = $("roam-target").value.trim();
  const minutes = parseFloat($("roam-minutes").value) || 10;
  const maxTokens = $("roam-tokens").value ? parseInt($("roam-tokens").value) : null;
  const res = await api().start_roam(target, minutes, maxTokens);
  if (!res.ok) { alert(res.error); return; }
  $("roam-start").disabled = true;
  $("roam-stop").disabled = false;
  $("live-view-btn").disabled = false;
  $("roam-state").textContent = "roaming";
  $("roam-log").textContent = "";
  $("roam-report").textContent = "";
  $("sb-state").textContent = "roaming…";
  clearInterval(polling);
  polling = setInterval(pollRoam, 900);
  startLiveStats();
}

async function stopRoam() { await api().stop_roam(); }

async function pollRoam() {
  const st = await api().roam_status();
  const log = $("roam-log");
  log.innerHTML = (st.log || [])
    .map((l) => l.includes("FINDING") ? `<div class="finding">${esc(l)}</div>` : `<div>${esc(l)}</div>`)
    .join("");
  log.scrollTop = log.scrollHeight;
  $("roam-findings").textContent = st.findings || 0;
  $("roam-toks").textContent = (st.tokens || {}).total_tokens || 0;
  if (!st.running) {
    clearInterval(polling);
    $("roam-start").disabled = false;
    $("roam-stop").disabled = true;
    $("live-view-btn").disabled = true;
    $("roam-state").textContent = "finished";
    $("sb-state").textContent = "idle";
    if (st.report)
      $("roam-report").innerHTML = `<span>report <b>${esc(st.report)}</b></span>`;
  }
}

/* ---- providers / tokens -------------------------------------------------------- */

async function checkProvider() {
  const btn = $("p-check");
  btn.disabled = true;
  btn.textContent = "Checking…";
  const res = await api().check_provider();
  btn.disabled = false;
  btn.textContent = "Check connection & vision";
  $("p-result").innerHTML =
    `<div class="check-result ${res.ok ? "ok" : "bad"}">${res.ok ? "✓" : "✗"} ${esc(res.detail)}</div>` +
    (res.ok && res.vision === false
      ? `<div class="warn-box">This model cannot do any vision-related testing —
         Argus will use the accessibility tree only. Pull a multimodal model
         (e.g. <b>ollama pull gemma3:9b</b>) for vision.</div>` : "");
}

async function refreshTokens() {
  try {
    const u = await api().token_usage();
    $("tok-top").textContent = u.session.total_tokens.toLocaleString();
    $("u-session").textContent = u.session.total_tokens.toLocaleString();
    $("u-project").textContent = u.project.total_tokens.toLocaleString();
    $("u-calls").textContent = (u.project.calls + u.session.calls).toLocaleString();
    $("sb-tokens").textContent = `${u.session.total_tokens.toLocaleString()} tokens`;
  } catch (e) { /* api not ready yet */ }
}

/* ---- knowledge ------------------------------------------------------- */

async function loadKnowledgeStats() {
  const target = $("ks-target").value.trim();
  if (!target) return;
  try {
    const stats = await api().knowledge_stats(target);
    $("ks-states").querySelector(".stat-val").textContent = stats.states ?? 0;
    $("ks-transitions").querySelector(".stat-val").textContent = stats.transitions ?? 0;
    $("ks-bugs").querySelector(".stat-val").textContent = stats.bugs ?? 0;
    $("ks-sessions").querySelector(".stat-val").textContent = stats.sessions ?? 0;
    $("ks-clear").disabled = false;
  } catch (e) {
    $("ks-states").querySelector(".stat-val").textContent = "—";
  }
}

async function clearKnowledge() {
  const target = $("ks-target").value.trim();
  if (!target) return;
  if (!confirm(`Reset all knowledge for "${target}"?`)) return;
  try {
    await api().knowledge_clear(target);
    $("ks-states").querySelector(".stat-val").textContent = "0";
    $("ks-transitions").querySelector(".stat-val").textContent = "0";
    $("ks-bugs").querySelector(".stat-val").textContent = "0";
    $("ks-sessions").querySelector(".stat-val").textContent = "0";
    $("ks-clear").disabled = true;
  } catch (e) { /* ignore */ }
}

/* ---- live preview & stats ------------------------------------------------- */

function toggleLiveView() {
  const card = $("live-view-card");
  const btn = $("live-view-btn");
  liveViewActive = !liveViewActive;
  if (liveViewActive) {
    card.style.display = "";
    btn.textContent = "Hide Live View";
    pollLiveView();
    liveViewInterval = setInterval(pollLiveView, 600);
  } else {
    card.style.display = "none";
    btn.textContent = "Show Live View";
    clearInterval(liveViewInterval);
    liveViewInterval = null;
  }
}

async function pollLiveView() {
  try {
    const res = await api().capture_live();
    if (res.b64) $("live-preview").src = "data:image/png;base64," + res.b64;
  } catch (e) { /* api not ready yet */ }
}

function startLiveStats() {
  $("ks-live-card").style.display = "";
  if (liveStatsInterval) clearInterval(liveStatsInterval);
  pollLiveStats();
  liveStatsInterval = setInterval(pollLiveStats, 5000);
}

function stopLiveStats() {
  clearInterval(liveStatsInterval);
  liveStatsInterval = null;
  $("ks-live-card").style.display = "none";
}

async function pollLiveStats() {
  try {
    const s = await api().live_stats();
    if (!s.active) { stopLiveStats(); return; }
    $("ks-live-states").textContent = s.states ?? 0;
    $("ks-live-transitions").textContent = s.transitions ?? 0;
    $("ks-live-bugs").textContent = s.bugs ?? 0;
  } catch (e) { /* ignore */ }
}

/* ---- utilities ------------------------------------------------------------ */

function badge(status, label) {
  return `<span class="badge ${status}"><span class="dot"></span>${esc(label)}</span>`;
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
