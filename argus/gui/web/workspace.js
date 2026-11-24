/* Native desktop workflows over the same project bridge as chat. */
"use strict";

const TEST_TYPES = [["cli", "Command"], ["browser", "Browser"], ["desktop-gui", "Desktop app"]];
const TEST_CHECKS = {
  cli: [["exit_code_is", "Exit code is"], ["stdout_contains", "Output contains"],
    ["stderr_contains", "Error output contains"]],
  browser: [["page_title_contains", "Page title contains"], ["url_contains", "URL contains"],
    ["text_visible", "Text is visible"]],
  "desktop-gui": [["text_visible", "Text is visible"], ["window_title_contains", "Window title contains"],
    ["element_exists", "Element exists"], ["process_running", "Process is running"],
    ["dialog_open", "Dialog is open"]],
};
const CAPSULE_PROVIDERS = [["auto", "Automatic"], ["libvirt", "libvirt / KVM"], ["hyperv", "Hyper-V"]];
let workspaceInputId = 0;

function workspaceButton(text, action, primary = false, extra = {}) {
  return h("button", { class: "btn hov" + (primary ? " workspace-primary" : ""), text, onClick: action, ...extra });
}

function statusLine() {
  return h("p", { class: "workspace-status", role: "status", "aria-live": "polite", hidden: true });
}

function workspaceStatus(line, message, style = "") {
  line.hidden = !message;
  line.textContent = message || "";
  line.className = "workspace-status" + (style ? " " + style : "");
}

function workspacePanel(title) {
  closeMenu();
  const dialog = $("workspace-dialog");
  $("workspace-title").textContent = title;
  $("workspace-context").textContent = state.info?.project_name || "Argus";
  const panel = h("div", { class: "workspace-panel" });
  $("workspace-content").replaceChildren(panel);
  $("workspace-content").scrollTop = 0;
  if (!dialog.open) dialog.showModal();
  return panel;
}

function closeWorkspace() {
  const dialog = $("workspace-dialog");
  if (dialog.open) dialog.close();
  // Discard secret fields and stop dialog-scoped polls.
  $("workspace-content").replaceChildren();
}

function hasWorkspace() {
  if (state.info && !state.info.project_required) return true;
  openProject();
  return false;
}

function workspaceAction(button, line, action, progress = "Working…") {
  if (button.disabled) return;
  button.disabled = true;
  state.pendingActions++;
  workspaceStatus(line, progress);
  return Promise.resolve().then(action).catch(() => {
    workspaceStatus(line, "The desktop service could not complete this request. Try again.", "error");
  }).finally(() => { button.disabled = false; state.pendingActions--; });
}

function choice(options, value, attrs = {}) {
  const input = h("select", attrs, options.map(([key, text]) => h("option", { value: key, text })));
  input.value = value;
  if (input.selectedIndex === -1 && options.length) input.selectedIndex = 0;
  return input;
}

function field(label, name, value, attrs = {}, options = null, browse = null, help = "") {
  const id = "workspace-field-" + ++workspaceInputId;
  const props = { id, name, ...attrs };
  const input = options ? choice(options, value, props) : h(attrs.rows ? "textarea" : "input", props);
  if (!options) input.value = value ?? "";
  const feedback = statusLine();
  const browseButton = browse ? workspaceButton("Browse…", (event) => workspaceAction(event.currentTarget, feedback, async () => {
    const result = await api().pick_input(typeof browse === "function" ? browse() : browse);
    if (!result.ok) return workspaceStatus(feedback, result.error, "error");
    if (result.path) {
      input.value = result.path;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    workspaceStatus(feedback, "");
  }, "Opening file picker…")) : null;
  if (help) input.setAttribute("aria-describedby", id + "-help");
  return h("div", { class: "workspace-field" }, h("label", { for: id, text: label }),
    h("div", { class: "workspace-input-row" }, input, browseButton),
    help ? h("small", { id: id + "-help", text: help }) : null, browse ? feedback : null);
}

function toggle(label, name, checked) {
  return h("label", { class: "workspace-toggle" }, h("input", { name, type: "checkbox", checked }), label);
}

function formValues(form) {
  return Object.fromEntries(new FormData(form));
}

function settingsTabs(section) {
  const tabs = [["model", "Model"], ["defaults", "Project"], ["runtime", "Runtime"], ["capsules", "Capsules"]];
  return h("nav", { class: "workspace-tabs", "aria-label": "Settings sections" },
    tabs.map(([key, text]) => workspaceButton(text, () => openSettings(key), false,
      { "aria-pressed": String(section === key) })));
}

async function openSettings(section = "model") {
  if (!hasWorkspace()) return;
  const panel = workspacePanel("Project settings");
  const body = h("div", { class: "workspace-body" }, h("p", { class: "workspace-muted", text: "Loading settings…" }));
  panel.append(settingsTabs(section), body);
  try {
    const config = await api().desktop_settings();
    if (!body.isConnected) return;
    if (!config.ok) {
      const status = statusLine();
      workspaceStatus(status, config.error, "error");
      body.replaceChildren(status);
      if (config.recoverable) {
        body.append(h("p", { text: "Argus will keep a private copy of your current settings. Tests, run history, and conversations stay available." }),
          workspaceButton("Restore project defaults", (event) => {
            if (!window.confirm("Back up these settings and restore the project defaults?")) return;
            workspaceAction(event.currentTarget, status, async () => {
              const result = await api().restore_desktop_settings(config.revision);
              if (!result.ok) return workspaceStatus(status, result.error, "error");
              await refreshInfo();
              await refreshTests();
              if (body.isConnected) openSettings(section);
            }, "Restoring project settings…");
          }, true));
      }
      return;
    }
    if (section === "model") renderModelSettings(body, config);
    else if (section === "defaults") renderProjectDefaults(body, config);
    else if (section === "runtime") await renderRuntimeSettings(body);
    else if (section === "capsules") await renderCapsuleSettings(body, config);
  } catch {
    body.replaceChildren(h("p", { class: "workspace-status error", role: "alert", text: "Could not load project settings." }));
  }
}

function renderModelSettings(body, config, selected = config.provider) {
  const provider = config.providers.find((item) => item.id === selected) || config.providers[0];
  const form = h("form", { class: "workspace-form", autocomplete: "off" });
  const status = statusLine();
  const providerField = field("Provider", "provider", provider.id, {}, config.providers.map((p) => [p.id, p.label]));
  providerField.querySelector("select").addEventListener("change", (event) => renderModelSettings(body, config, event.target.value));
  const endpointPlaceholder = {
    ollama: "http://localhost:11434", openai: "https://api.openai.com/v1",
    gemini: "Google Gemini OpenAI-compatible endpoint", azure: "https://your-resource.openai.azure.com/openai/v1",
    litellm: "https://your-provider.example/v1",
  };
  const hosted = provider.id !== "ollama";
  const endpointRequired = ["azure", "litellm"].includes(provider.id);
  const submit = workspaceButton("Save model settings", null, true, { type: "submit" });
  const check = workspaceButton("Check saved model", (event) => workspaceAction(event.currentTarget, status, async () => {
    const result = await api().test_model_connection(provider.id);
    workspaceStatus(status, result.ok ? "Connected to " + result.model + " · Vision: " + result.vision : result.error,
      result.ok ? "success" : "error");
  }, "Checking the model…"));
  form.append(h("h2", { text: "Model connection" }),
    h("p", { class: "workspace-muted", text: "Argus uses a model for Roam, test drafts, and instructions. Explicit checks can run on their own." }),
    config.provider_pin ? h("p", { class: "workspace-status", text: "Host policy selects " + config.effective_provider + " · " + config.effective_model + "." }) : "",
    h("div", { class: "workspace-grid" }, providerField, field("Model or deployment ID", "model", provider.model, { required: true })),
    provider.id !== "anthropic" ? field("Endpoint", "endpoint", provider.endpoint,
      { placeholder: endpointPlaceholder[provider.id], required: endpointRequired, type: "url" }) : "",
    hosted ? field("API key", "api_key", "", { type: "password", autocomplete: "new-password",
      maxlength: 4096, placeholder: provider.has_key ? "Credential available" : "Enter API key" }, null, null,
    provider.has_key ? provider.key_source + ". A blank field keeps the current key." : "Saved in the per-user Argus secret store.") : "",
    hosted && provider.has_key ? toggle("Clear the saved project credential", "clear_key", false) : "",
    toggle("Use as the default provider", "use_by_default", provider.id === config.provider),
    h("div", { class: "workspace-actions" }, submit, check), status);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues(form);
    values.use_by_default = values.use_by_default === "on";
    values.clear_key = values.clear_key === "on";
    workspaceAction(submit, status, async () => {
      const result = await api().save_desktop_settings("model", config.revision, values);
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      await refreshInfo();
      if (!form.isConnected) return;
      renderModelSettings(body, result, provider.id);
      workspaceStatus(body.querySelector('[role="status"]'), "Model settings saved.", "success");
    }, "Saving model settings…");
  });
  body.replaceChildren(form);
}

function renderProjectDefaults(body, config) {
  const form = h("form", { class: "workspace-form" });
  const submit = workspaceButton("Save project defaults", null, true, { type: "submit" });
  const status = statusLine();
  const backend = field("Memory backend", "knowledge_type", config.knowledge.type === "qdrant" ? "docker" : config.knowledge.type,
    {}, [["json", "Local keyword search"], ["auto", "Automatic"], ["local", "Local vector search"],
      ["docker", "Project-managed Qdrant"], ["external", "External Qdrant"]]);
  const endpoint = field("Qdrant endpoint", "vector_url", config.knowledge.vector_url, { type: "url" });
  const embedding = field("Embedding model", "embedding_model", config.knowledge.embedding_model);
  const backendSelect = backend.querySelector("select");
  function updateBackend() {
    endpoint.hidden = backendSelect.value !== "external";
    endpoint.querySelector("input").required = !endpoint.hidden;
    embedding.hidden = backendSelect.value === "json";
  }
  backendSelect.addEventListener("change", updateBackend);
  updateBackend();
  form.append(h("h2", { text: "Execution defaults" }),
    field("Run tests on", "environment", config.environment, {},
      [["local", "Local machine"], ["capsule", "Capsule virtual machine"]]),
    config.environment_pin ? h("p", { class: "workspace-muted", text: "Host policy controls the active execution location." }) : "",
    h("div", { class: "workspace-grid" },
      field("Time budget (minutes)", "time_minutes", config.time_minutes, { type: "number", min: ".01", max: 240, step: "any" }, null, null, "Leave blank for no test time limit."),
      field("Token budget", "max_tokens", config.max_tokens, { type: "number", min: 1, step: 1 }, null, null, "Leave blank to use the provider default.")),
    toggle("Retain a Failure Capsule when a Capsule test fails", "retain", config.capsule.retain_on_failure),
    h("h2", { text: "Project memory" }),
    toggle("Store observations and findings across runs", "memory", config.knowledge.enabled),
    backend, endpoint, embedding,
    h("div", { class: "workspace-actions" }, submit), status);
  let revision = config.revision;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues(form);
    for (const name of ["retain", "memory"]) values[name] = values[name] === "on";
    workspaceAction(submit, status, async () => {
      const result = await api().save_desktop_settings("defaults", revision, values);
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      revision = result.revision;
      await refreshInfo();
      workspaceStatus(status, "Project defaults saved.", "success");
    }, "Saving project defaults…");
  });
  body.replaceChildren(form);
}

async function renderRuntimeSettings(body) {
  const result = await api().runtime_status();
  if (!body.isConnected) return;
  const status = statusLine();
  const launch = workspaceButton(result.browser.state === "installable" ? "Install Chromium" : "Check Chromium",
    (event) => workspaceAction(event.currentTarget, status, async () => {
      const wasInstall = result.browser.state === "installable";
      const reply = await api().chromium(wasInstall);
      workspaceStatus(status, reply.ok ? reply.detail : reply.error, reply.ok ? "success" : "error");
      if (reply.ok && wasInstall) {
        result.browser.state = "installed";
        launch.textContent = "Check Chromium";
      }
    }, result.browser.state === "installable" ? "Downloading and checking Chromium…" : "Starting Chromium…"), true);
  body.replaceChildren(h("div", { class: "workspace-form" },
    h("div", { class: "workspace-card" }, h("h2", { text: "Browser tests" }),
      h("p", { class: "workspace-muted", text: result.browser.detail }),
      result.browser.state !== "unavailable" ? h("div", { class: "workspace-actions" }, launch) : "", status),
    h("div", { class: "workspace-card" }, h("h2", { text: "Desktop app tests" }),
      h("p", { class: "workspace-muted", text: result.desktop.detail })),
    h("div", { class: "workspace-card" }, h("h2", { text: "Command tests" }),
      h("p", { class: "workspace-muted", text: "Run commands and check their output in the project folder." }))));
}

function openTestLibrary() {
  if (!hasWorkspace()) return;
  const panel = workspacePanel("Tests");
  const filter = h("input", { type: "search", placeholder: "Find a test…", "aria-label": "Find a test" });
  const list = h("div", { class: "workspace-list" });
  const actions = h("div", { class: "workspace-actions" },
    workspaceButton("New test", () => openTestEditor(), true),
    workspaceButton("Run all", () => { closeWorkspace(); runChip("Run all tests", I("run", { tests: "all" })); },
      false, { disabled: !state.tests.length }),
    workspaceButton(state.watchOn ? "Stop watching" : "Watch for changes", () => {
      closeWorkspace();
      runChip(state.watchOn ? "Stop watching" : "Watch for changes", I("watch", { action: state.watchOn ? "stop" : "start" }));
    }));
  panel.append(actions, filter, list);
  function renderList() {
    const query = filter.value.trim().toLowerCase();
    const rows = state.tests.filter((test) => (test.name + " " + test.file).toLowerCase().includes(query));
    list.replaceChildren(...rows.map((test) => h("div", { class: "workspace-list-row" },
      h("div", { class: "workspace-row-title" }, h("strong", { text: test.name }),
        h("small", { text: test.error || adapterShort(test.adapter) + " · " + test.steps + " steps" })),
      test.last ? h("span", { class: "badge " + test.last, text: STATUS_LABEL[test.last] || test.last }) : "",
      workspaceButton("Edit", () => openTestEditor(test.file), false, { disabled: !!test.error, "aria-label": "Edit " + test.name }),
      workspaceButton("Preview", () => { closeWorkspace(); runChip("Preview " + test.name, I("dry_run", { tests: [test.file] })); },
        false, { disabled: !!test.error, "aria-label": "Preview " + test.name }),
      workspaceButton("Run", () => { closeWorkspace(); runChip("Run " + test.name, I("run", { tests: [test.file] })); },
        false, { disabled: !!test.error, "aria-label": "Run " + test.name }))));
    if (!rows.length) list.append(h("p", { class: "workspace-muted", text: query ? "No matching tests." : "Create a test to get started." }));
  }
  filter.addEventListener("input", renderList);
  renderList();
}

function targetField(testType, value = "", allowFileUrl = false) {
  const browser = testType === "browser";
  const cli = testType === "cli";
  return field(browser ? "Website URL" : (cli ? "Command" : "Application to launch"), "target", value, {
    required: true, maxlength: 4096, placeholder: browser ? "https://example.com" : (cli ? "python app.py --version" : "Choose an executable"),
  }, null, browser ? null : (cli ? "cli-command" : "desktop-command"),
  browser && allowFileUrl ? "You can also use a file:// URL for a local test fixture." :
    (cli ? "Commands run from the project folder for Local tests. Add arguments after the executable." : ""));
}

async function openTestEditor(file = "", draft = "") {
  if (!hasWorkspace()) return;
  const panel = workspacePanel(file ? "Edit test" : (draft ? "Review test draft" : "New test"));
  const status = statusLine();
  panel.append(status);
  workspaceStatus(status, "Loading test…");
  let reply;
  try { reply = await api().test_form(file, draft); }
  catch { return workspaceStatus(status, "Could not open this test.", "error"); }
  if (!panel.isConnected) return;
  if (!reply.ok) return workspaceStatus(status, reply.error, "error");
  workspaceStatus(status, "");
  const source = reply.form;
  const form = h("form", { class: "workspace-form" });
  const adapterField = field("Test type", "adapter", source.adapter, {}, TEST_TYPES);
  const adapter = adapterField.querySelector("select");
  const target = h("div", {}, targetField(adapter.value, source.target, true));
  const plan = h("div", { class: "workspace-plan" });
  const transfers = h("div", { class: "workspace-plan" });
  const rows = source.rows.slice();
  const staging = source.staging.map((row) => ({ ...row }));
  const actions = h("div", { class: "workspace-actions" });
  const save = workspaceButton("Save test", null, true, { type: "submit" });
  const saveRun = workspaceButton("Save & run", null, false, { type: "submit" });
  actions.append(save, saveRun, workspaceButton("Back to tests", openTestLibrary));
  form.append(h("div", { class: "workspace-grid" }, field("Test name", "name", source.name, { required: true, maxlength: 200 }), adapterField),
    target, h("div", { class: "workspace-section-head" }, h("h2", { text: "Steps and checks" }),
      h("span", { class: "workspace-muted", text: "Setup → Test → Teardown" })),
    plan, h("div", { class: "workspace-actions" },
      workspaceButton("Add check", () => addStep("assert")),
      workspaceButton("Add instruction", () => addStep("instruction"))),
    h("details", { class: "workspace-details" }, h("summary", { text: "Retries and Capsule files" }),
      field("Retries", "retries", source.retries, { type: "number", min: 0, max: 20, step: 1, required: true }),
      toggle("Continue after a failed check", "continue_on_failure", source.continue_on_failure),
      h("h3", { text: "Files to copy into a Capsule" }), transfers,
      workspaceButton("Add a project file", () => { staging.push({ source: "", destination: "", sha256: "" }); renderTransfers(); }),
      field("Artifacts to collect (one guest path per line)", "collect", source.collect.join("\n"), { rows: 3 })),
    state.watchOn ? h("p", { class: "workspace-status", text: "Watch mode is active. Saving a test can run it immediately." }) : "",
    actions, status);
  panel.replaceChildren(form);
  adapter.addEventListener("change", () => {
    const value = target.querySelector("input").value;
    target.replaceChildren(targetField(adapter.value, value, true));
    for (const row of rows) {
      if (row.kind === "assert" && !TEST_CHECKS[adapter.value].some(([key]) => key === row.assertion)) {
        row.assertion = TEST_CHECKS[adapter.value][0][0];
        row.value = row.assertion === "exit_code_is" ? "0" : "";
      }
    }
    renderRows();
  });

  function addStep(kind) {
    const assertion = TEST_CHECKS[adapter.value][0][0];
    rows.push({ phase: "steps", kind, assertion, value: kind === "assert" && assertion === "exit_code_is" ? "0" : "" });
    renderRows();
    plan.lastElementChild?.querySelector("select")?.focus();
  }

  function renderRows() {
    plan.replaceChildren(...rows.map((row, index) => {
      const phase = choice([["setup", "Setup"], ["steps", "Test"], ["teardown", "Teardown"]], row.phase,
        { "aria-label": "Phase of step " + (index + 1) });
      phase.addEventListener("change", () => { row.phase = phase.value; });
      let checkType = null;
      if (row.kind === "assert") {
        checkType = choice(TEST_CHECKS[adapter.value], row.assertion, { "aria-label": "Check " + (index + 1) });
        checkType.addEventListener("change", () => {
          row.assertion = checkType.value;
          row.value = row.assertion === "exit_code_is" ? "0" : (row.assertion === "process_running" ? "true" : "");
          renderRows();
          plan.children[index].querySelector("select:nth-child(2)")?.focus();
        });
      }
      const setValue = (event) => { row.value = event.target.value; };
      const label = (row.kind === "assert" ? "Expected value for step " : "Instruction for step ") + (index + 1);
      let expected;
      if (row.assertion === "process_running" && row.kind === "assert") {
        expected = choice([["true", "Running"], ["false", "Stopped"]], String(row.value), { "aria-label": label, onChange: setValue });
      } else if (row.assertion === "element_exists" && row.kind === "assert") {
        if (typeof row.value !== "object" || !row.value) row.value = { name: row.value || "", control_type: "" };
        expected = h("div", { class: "workspace-grid" },
          h("input", { "aria-label": "Element name", placeholder: "Element name", value: row.value.name || "",
            onInput: (event) => { row.value.name = event.target.value; } }),
          h("input", { "aria-label": "Control type", placeholder: "Control type (optional)", value: row.value.control_type || "",
            onInput: (event) => { row.value.control_type = event.target.value; } }));
      } else {
        expected = h("input", { "aria-label": label, type: row.kind === "assert" && row.assertion === "exit_code_is" ? "number" : "text",
          required: true, value: row.value ?? "", placeholder: label, onInput: setValue });
      }
      return h("div", { class: "workspace-step" },
        h("div", { class: "workspace-step-head" }, phase, checkType || h("span", { class: "workspace-muted", text: "Model instruction" }),
          h("span", { class: "workspace-step-actions" },
            workspaceButton("↑", () => moveRow(index, -1), false, { disabled: index === 0, "aria-label": "Move step " + (index + 1) + " up" }),
            workspaceButton("↓", () => moveRow(index, 1), false, { disabled: index === rows.length - 1, "aria-label": "Move step " + (index + 1) + " down" }),
            workspaceButton("Remove", () => { rows.splice(index, 1); renderRows(); }, false, { "aria-label": "Remove step " + (index + 1) }))),
        expected);
    }));
  }

  function moveRow(index, offset) {
    const destination = index + offset;
    [rows[destination], rows[index]] = [rows[index], rows[destination]];
    renderRows();
    plan.children[destination].querySelector("select").focus();
  }

  function renderTransfers() {
    transfers.replaceChildren(...staging.map((row, index) => h("div", { class: "workspace-step" },
      h("div", { class: "workspace-grid" },
        field("Project-relative file", "", row.source), field("Destination in guest", "", row.destination)),
      field("Approved SHA-256 (optional)", "", row.sha256),
      workspaceButton("Remove file", () => { staging.splice(index, 1); renderTransfers(); }))));
    transfers.querySelectorAll(".workspace-step").forEach((item, index) => {
      item.querySelectorAll("input").forEach((input, j) => input.addEventListener("input", () => {
        staging[index][["source", "destination", "sha256"][j]] = input.value;
      }));
    });
  }

  let saving = false;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    const button = event.submitter || save;
    const other = button === saveRun ? save : saveRun;
    other.disabled = true;
    const value = formValues(form);
    Object.assign(value, { rows, staging, collect: value.collect.split("\n").map((path) => path.trim()).filter(Boolean),
      continue_on_failure: value.continue_on_failure === "on" });
    workspaceAction(button, status, async () => {
      const result = await api().save_test_form(value, file, reply.revision || "", draft);
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      if (draft) markDraftSaved(draft, result.file);
      await refreshTests();
      if (!form.isConnected) return;
      closeWorkspace();
      if (button === saveRun) runChip("Run " + value.name, I("run", { tests: [result.file] }));
      else {
        openTestLibrary();
        const saved = statusLine();
        workspaceStatus(saved, "Saved " + value.name + ".", "success");
        $("workspace-content").firstElementChild.prepend(saved);
      }
    }, "Saving test…")?.finally(() => { saving = false; other.disabled = false; });
  });
  renderRows();
  renderTransfers();
  form.elements.namedItem("name").focus();
}

function openRoamForm() {
  if (!hasWorkspace()) return;
  const panel = workspacePanel("Start Roam");
  const form = h("form", { class: "workspace-form" });
  const last = state.info.last_target || "";
  const adapterField = field("Target type", "adapter", /^https?:\/\//i.test(last) ? "browser" : "cli", {}, TEST_TYPES);
  const adapter = adapterField.querySelector("select");
  const target = h("div", {}, targetField(adapter.value, last));
  adapter.addEventListener("change", () => target.replaceChildren(targetField(adapter.value, target.querySelector("input").value)));
  const launch = workspaceButton("Start Roam", null, true, { type: "submit" });
  form.append(h("p", { class: "workspace-muted", text: "Let Argus explore a target using the selected model." }),
    h("div", { class: "workspace-grid" }, adapterField,
      field("Duration (minutes)", "minutes", state.info.time_minutes || 10, {
        type: "number", min: ".01", max: 240, step: "any", required: true,
      })),
    target, toggle("Use project memory", "memory", state.info.memory),
    h("div", { class: "workspace-context-bar" }, h("span", { text: state.info.env_label }),
      h("span", { text: state.info.provider + " · " + state.info.model })),
    !state.info.model_credential_ready ? h("p", { class: "workspace-status error", text: "Check your model connection in Settings before starting Roam." }) : "",
    h("div", { class: "workspace-actions" }, launch, workspaceButton("Model settings", () => openSettings("model"))));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = formValues(form);
    closeWorkspace();
    runChip("Roam " + value.target, I("roam", {
      target: value.target, adapter: value.adapter, minutes: Number(value.minutes), memory: value.memory === "on",
    }));
  });
  panel.append(form);
}

async function openRunHistory() {
  if (!hasWorkspace()) return;
  const panel = workspacePanel("Run history");
  const status = statusLine();
  workspaceStatus(status, "Loading results…");
  panel.append(status);
  try {
    const runs = await api().recent_runs(60);
    if (!panel.isConnected) return;
    if (!runs.length) return workspaceStatus(status, "Run a test to see its result and evidence here.");
    panel.replaceChildren(h("div", { class: "workspace-list" }, runs.map((run) => h("div", { class: "workspace-list-row" },
      h("div", { class: "workspace-row-title" }, h("strong", { text: run.test.replace(/\.test\.yaml$/, "") }),
        h("small", { text: run.steps + " checks passed · " + run.duration + "s · " + fmt(run.tokens) + " tokens" })),
      h("span", { class: "badge " + run.status, text: STATUS_LABEL[run.status] || run.status }),
      run.evidence_key ? workspaceButton("Evidence", () => runEvidence(run.evidence_key)) : "",
      run.retained ? workspaceButton("Inspect Capsule", () => inspectRetainedCapsule(run.id)) : ""))));
  } catch {
    workspaceStatus(status, "Run history could not be loaded.", "error");
  }
}

function runEvidence(key) {
  closeWorkspace();
  return runChip("Verify run evidence", I("evidence", { key }));
}

function inspectRetainedCapsule(key) {
  const panel = workspacePanel("Inspect Failure Capsule");
  const status = statusLine();
  panel.append(h("p", { text: "Argus will reconnect to the same virtual machine, capture an observation, and return it to quarantine." }),
    workspaceButton("Reconnect and inspect", (event) => workspaceAction(event.currentTarget, status, async () => {
      const reply = await api().inspect_failure_capsule(key);
      if (!reply.ok) return workspaceStatus(status, reply.error, "error");
      panel.replaceChildren(h("p", { class: "workspace-status success", text: "Capsule quarantined · Generation " + reply.generation }),
        h("p", { class: "workspace-muted", text: reply.title || reply.capsule_id }),
        reply.screenshot ? h("img", { class: "workspace-capture", alt: "Retained Capsule observation",
          src: "data:image/png;base64," + reply.screenshot }) : "",
        workspaceButton("Run history", openRunHistory));
    }, "Reconnecting to the retained Capsule…"), true), status);
}

async function renderCapsuleSettings(body, config) {
  const cc = config.capsule;
  const detail = h("div", { class: "workspace-form" });
  const status = statusLine();
  const actions = h("div", { class: "workspace-actions" });
  const header = h("div", { class: "workspace-card" }, h("h2", { text: "Capsule runtime" }),
    h("p", { class: "workspace-muted", text: cc.environment_definition
      ? "A published image is selected."
      : (cc.image ? "A prepared disk is selected." : "Select a prepared disk or build a verified image.") }),
    h("div", { class: "workspace-actions" },
      workspaceButton("Check readiness", (event) => workspaceAction(event.currentTarget, status, async () => {
        const result = await api().environment(true);
        const verified = result.rows?.some((row) => row.k === "configuration" && row.v.startsWith("verified"));
        const error = result.rows?.some((row) => row.k === "configuration" && row.v === "not ready");
        workspaceStatus(status, verified ? "Image and control settings verified. The VM starts when you run a test." :
          (result.error || (error ? result.note : "Host policy selects Local execution.")), verified ? "success" : "error");
      }, "Checking the image and control settings…")),
      workspaceButton("Execution defaults", () => openSettings("defaults"))),
    status);
  const tabs = [["disk", "Prepared disk"], ["build", "Build from ISO"], ["published", "Published image"]];
  function select(tab) {
    actions.replaceChildren(...tabs.map(([key, text]) => workspaceButton(text, () => select(key), false,
      { "aria-pressed": String(tab === key) })));
    if (tab === "disk") renderDiskForm(detail, config);
    else if (tab === "published") renderPublishedForm(detail, config);
    else renderBuildForm(detail, config);
  }
  body.replaceChildren(header, actions, detail);
  const build = await api().image_build_status();
  if (!body.isConnected) return;
  select(build.state === "running" || build.state === "ready" ? "build" : cc.environment_definition ? "published" : "disk");
}

function renderDiskForm(detail, config) {
  const cc = config.capsule;
  const form = h("form", { class: "workspace-form" });
  const status = statusLine();
  const submit = workspaceButton("Save prepared disk", null, true, { type: "submit" });
  form.append(h("h2", { text: "Use a prepared guest" }),
    h("p", { class: "workspace-muted", text: "Select a virtual disk that already contains the Argus guest runtime." }),
    field("Hypervisor", "provider", cc.provider, {}, CAPSULE_PROVIDERS),
    field("Virtual disk", "image", cc.image, { required: true }, null, "disk"),
    field("Guest HTTPS CA certificate", "guest_ca_cert", cc.guest_ca_cert, { required: true }, null, "ca"),
    field("Guest bootstrap token", "guest_token", "", { type: "password", autocomplete: "new-password", maxlength: 4096 },
      null, null, "Leave blank to keep the configured credential."),
    field("VM storage folder", "vm_root", cc.vm_root, {}, null, "folder"),
    h("div", { class: "workspace-grid" },
      field("Hyper-V internal switch (if used)", "switch_name", cc.switch_name),
      field("Linux QEMU access group (if used)", "libvirt_qemu_group", cc.libvirt_qemu_group)),
    h("div", { class: "workspace-actions" }, submit), status);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    workspaceAction(submit, status, async () => {
      const result = await api().save_desktop_settings("disk", config.revision, formValues(form));
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      await refreshInfo();
      if (form.isConnected) {
        config.revision = result.revision;
        form.elements.namedItem("guest_token").value = "";
        workspaceStatus(status, "Prepared disk selected. Use Check readiness to verify the connection settings.", "success");
      }
    }, "Saving Capsule settings…");
  });
  detail.replaceChildren(form);
}

function renderPublishedForm(detail, config) {
  const cc = config.capsule;
  const form = h("form", { class: "workspace-form" });
  const status = statusLine();
  const submit = workspaceButton("Verify and select image", null, true, { type: "submit" });
  form.append(h("h2", { text: "Use a published image" }),
    h("p", { class: "workspace-muted", text: "Argus verifies the image and its ATES evidence before making it available to Capsule tests." }),
    field("Environment definition", "environment_definition", cc.environment_definition, { required: true }, null, "definition"),
    field("Image library", "image_cache_root", cc.image_cache_root, { required: true }, null, "folder"),
    field("Provisioning evidence folder", "provisioning_evidence_root", cc.provisioning_evidence_root, {}, null, "folder",
      "Leave blank if evidence is kept in the image library."),
    field("VM storage folder", "vm_root", cc.vm_root, {}, null, "folder"),
    h("div", { class: "workspace-grid" }, field("Hypervisor", "provider", cc.provider, {}, CAPSULE_PROVIDERS),
      field("Disk format", "image_format", cc.image_format, {}, [["", "Automatic"], ["qcow2", "QCOW2"], ["raw", "RAW"], ["vhdx", "VHDX"]]),
      field("Hyper-V internal switch (if used)", "switch_name", cc.switch_name),
      field("Linux QEMU access group (if used)", "libvirt_qemu_group", cc.libvirt_qemu_group)),
    h("div", { class: "workspace-actions" }, submit), status);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    workspaceAction(submit, status, async () => {
      const result = await api().select_image(config.revision, formValues(form));
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      config.revision = result.revision;
      await refreshInfo();
      workspaceStatus(status, "Verified image selected. Choose Capsule under Execution defaults when ready to run.", "success");
    }, "Verifying image and evidence…");
  });
  detail.replaceChildren(form);
}

const BUILD_STAGES = {
  preflight: "Checking the selected files and host",
  cache_verification: "Verifying the existing image and evidence",
  media_verified: "Verifying the ISO and offline guest runtime",
  provider_selected: "Checking the hypervisor contract",
  installation: "Installing the OS and Argus runtime",
  baseline_validated: "Booting disposable Capsules to verify the image",
  image_hashed: "Hashing the immutable image",
  publication: "Publishing and verifying ATES evidence",
};

async function renderBuildForm(detail, config) {
  const slot = h("div", { class: "workspace-form" }, h("p", { class: "workspace-muted", text: "Checking host capabilities…" }));
  detail.replaceChildren(slot);
  try {
    const build = await api().image_build_status();
    if (!slot.isConnected) return;
    const status = statusLine();
    if (build.state === "running") {
      const cancel = workspaceButton("Cancel build", (event) => workspaceAction(event.currentTarget, status, async () => {
        const reply = await api().cancel_image_build();
        workspaceStatus(status, reply.ok ? "Cancellation requested. Waiting for verified cleanup." : reply.error,
          reply.ok ? "" : "error");
      }, "Requesting cancellation…"), false, { disabled: build.cancelling || build.stage === "publication" });
      workspaceStatus(status, build.cancelling ? "Waiting for verified cleanup…" : BUILD_STAGES[build.stage] || BUILD_STAGES.preflight);
      slot.replaceChildren(h("h2", { text: "Image build in progress" }), h("progress", { "aria-label": "Image build in progress" }), status, cancel);
      setTimeout(() => { if (slot.isConnected) renderBuildForm(detail, config); }, 1200);
      return;
    }
    if (build.state === "ready") {
      const use = workspaceButton("Use verified image", (event) => workspaceAction(event.currentTarget, status, async () => {
        const result = await api().select_image(config.revision);
        if (!result.ok) return workspaceStatus(status, result.error, "error");
        await refreshInfo();
        config.revision = result.revision;
        workspaceStatus(status, "Image selected. Choose Capsule under Execution defaults when ready to run.", "success");
      }, "Verifying the published image…"), true);
      slot.replaceChildren(h("h2", { text: build.name }), h("p", { class: "workspace-status success", text: "Image and ATES evidence verified." }),
        h("dl", { class: "workspace-facts" },
          h("dt", { text: "Environment" }), h("dd", { text: build.environment_id }),
          h("dt", { text: "Image SHA-256" }), h("dd", { text: build.image_sha256 }),
          h("dt", { text: "ATES run" }), h("dd", { text: build.evidence_run_id })),
        h("div", { class: "workspace-actions" }, use, workspaceButton("Build another", () => buildInputs(slot, build.hosts, config))), status);
      return;
    }
    buildInputs(slot, build.hosts, config, build);
  } catch {
    slot.replaceChildren(h("p", { class: "workspace-status error", role: "alert", text: "Could not check image build status." }));
  }
}

function buildInputs(slot, hosts, config, previous = {}) {
  const form = h("form", { class: "workspace-form" });
  const profile = field("Operating system", "os", "ubuntu", {}, [["ubuntu", "Ubuntu x86-64"], ["windows-11", "Windows 11 x86-64"]]);
  const status = statusLine();
  const readiness = statusLine();
  const fields = h("fieldset", { class: "workspace-build-fields" });
  const submit = workspaceButton("Build verified image", null, true, { type: "submit" });
  const cc = config.capsule;
  const suffix = { required: true, minlength: 64, maxlength: 64, pattern: "[0-9a-fA-F]{64}" };
  const iso = field("Installation ISO", "iso", "", { required: true }, null, "iso");
  const bundle = field("Approved offline guest runtime ZIP", "runtime", "", { required: true }, null, "runtime");
  const preset = h("div", { class: "workspace-grid" });
  fields.append(field("Image name", "name", "", { required: true, maxlength: 100 }), preset,
    h("div", { class: "workspace-grid" }, iso, field("Approved ISO SHA-256", "iso_sha256", "", suffix),
      bundle, field("Approved runtime SHA-256", "runtime_sha256", "", suffix)),
    field("Runtime version", "runtime_version", "", { required: true, placeholder: "Version from the runtime bundle" }),
    h("div", { class: "workspace-grid workspace-three" },
      field("Virtual CPUs", "cpu_count", 4, { required: true, type: "number", min: 1 }),
      field("RAM (MiB)", "memory_mb", 4096, { required: true, type: "number", min: 1024 }),
      field("Disk size (GiB)", "disk_size_gib", 40, { required: true, type: "number", min: 16 })),
    h("div", { class: "workspace-grid" },
      field("Image library", "cache_root", cc.image_cache_root, { required: true }, null, "folder"),
      field("VM storage folder", "vm_root", cc.vm_root, { required: true }, null, "folder")));
  form.append(h("h2", { text: "Build from an approved ISO" }),
    h("p", { class: "workspace-muted", text: "Choose a host-only network and supply approved digests for your installation media and offline runtime." }),
    profile, readiness, fields, h("div", { class: "workspace-actions" }, submit), status);
  const profileSelect = profile.querySelector("select");
  function renderProfile() {
    const windows = profileSelect.value === "windows-11";
    const blocker = hosts[profileSelect.value];
    workspaceStatus(readiness, blocker, "error");
    fields.disabled = !!blocker;
    submit.disabled = !!blocker;
    preset.replaceChildren(
      windows ? field("Release", "release", "24H2", {}, [["24H2", "24H2"], ["23H2", "23H2"]])
        : field("Release", "release", "24.04", { required: true }),
      windows ? field("Edition", "edition", "professional", {},
        [["professional", "Professional"], ["education", "Education"], ["enterprise", "Enterprise"]])
        : field("Flavor", "flavor", "server", {}, [["server", "Server"], ["desktop", "Desktop"]]),
      field("Disk format", "format", windows ? "vhdx" : "qcow2", {},
        windows ? [["vhdx", "VHDX"]] : [["qcow2", "QCOW2"], ["raw", "RAW"]]),
      field("Host-only network", "network", windows ? cc.switch_name : cc.network_name, { required: true }),
      windows ? "" : field("Linux QEMU access group", "qemu_group", cc.libvirt_qemu_group, { required: true }),
      windows ? "" : field("Host-reachable Ubuntu package mirror", "mirror", "", { required: true, type: "url" }));
  }
  profileSelect.addEventListener("change", renderProfile);
  renderProfile();
  if (previous.error) workspaceStatus(status, previous.error, "error");
  if (previous.state === "cancelled") workspaceStatus(status, "Image build cancelled.");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    workspaceAction(submit, status, async () => {
      const result = await api().start_image_build(formValues(form));
      if (!result.ok) return workspaceStatus(status, result.error, "error");
      if (slot.isConnected) renderBuildForm(slot.parentElement, config);
    }, "Starting the image build…");
  });
  slot.replaceChildren(form);
}
