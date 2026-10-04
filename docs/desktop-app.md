# Desktop app

The desktop app uses the same runner, adapters, execution environments, knowledge store, and ATES authority as the CLI. Its chat interface shows test results, drafted YAML, provider checks, token usage, run history, knowledge, and evidence in the conversation. The live panel shows progress and available screenshots during execution.

## Open a project and run the first test

Install a desktop package from [GitHub Releases](https://github.com/Varun-SV/Argus/releases/latest), or install the Python GUI extra and run:

```bash
pip install "argus-app-testing[gui]"
argus gui
```

Python installations also need the appropriate [target adapter dependencies](../README.md#python-package). Browser targets require `playwright install chromium`. The native desktop window needs a supported pywebview backend; Windows uses WebView2, and Linux requires its GTK/WebKit system packages.

1. Click **Open project folder**, beside the project name at the bottom of the sidebar. A folder opens in a separate window with its own jobs and session settings. Launching from a folder containing `.argus/` opens that project directly.
2. For a new folder, choose **Set up this project**. Argus creates `.argus/config.yaml` and a small `smoke.test.yaml`. Existing files are preserved.
3. Click `smoke.test.yaml` in the sidebar to run it. It starts the platform's echo command and verifies its output and exit code using structured assertions. This sample requires no model call.
4. Open **Evidence** on the completed result to verify the canonical ATES manifest. Use **Run history** to inspect persisted results.

Project initialization is explicit. Argus does not initialize its installation directory or an arbitrary launch directory when no project is selected. Configuration errors appear in the conversation; fix `.argus/config.yaml` and choose **Retry settings**.

## Configure a model

Edit `.argus/config.yaml` to configure Ollama, OpenAI, or Anthropic. Set hosted-provider credentials through the supported configuration or environment variables. Use `/providers` to check connection and vision support before running natural-language actions.

The model picker changes the provider for this window's session without rewriting the project configuration. Explicit environment-variable policy takes precedence. Free text uses a model call to choose from a validated intent set; slash commands and menu actions skip that routing call. Model-dependent actions such as drafting tests still call the selected provider. Structured assertions execute deterministically.

## Work through chat

Use **What can Argus do?** or `/help` for supported commands and examples:

| Action | Command |
|---|---|
| Run tests | `/run` or `/run smoke.test.yaml` |
| Preview tests | `/dry-run` |
| Draft a test | `/write check that the login page rejects a wrong password` |
| Explore a target | `/roam "python tool.py --flag" --adapter cli --minutes 5` |
| Watch test files | `/watch` |
| Check provider | `/providers` |
| Inspect usage | `/tokens` |
| Inspect history | `/report` |
| Inspect execution settings | `/env` |
| Verify evidence | `/evidence` |

Drafts are validated before they are displayed. Review their YAML, preview it, and explicitly choose **Save to .argus**. Saving never replaces an existing test. Unsaved drafts belong to the current application session; after restarting, draft text remains in the conversation but its action buttons may ask you to draft it again.

**Stop** requests cancellation and lets execution perform its normal teardown. Closing a window with an active job requests Stop and keeps the window open until cleanup finishes; close it again after the result settles. Watch mode observes `.argus/*.test.yaml` and re-runs changed files. Stop watching before editing tests you do not want to execute.

Conversations are stored in per-user application data and separated by project. A window reload reconnects to jobs and watch mode still owned by the current backend. After a process restart, an interrupted job is shown as **Outcome unknown**; check persisted history and ATES evidence before retrying. Malformed saved cards show a recovery message instead of preventing startup. Conversation saving errors are surfaced before closing.

## Capsule readiness

The environment picker selects Local or Capsule execution for the session. Local execution shares the host; Capsule execution requires a configured provider, supported virtualization host, and prepared image. Selecting Capsule never silently falls back to Local.

`/env` distinguishes manually prepared images from verified ISO-derived cache entries and reports configuration blockers. This inspection does not allocate a VM or contact a guest. VM and guest readiness are checked when a run starts.

ISO-derived images retain the [PR #27 provisioning contracts](os-environment-provisioning.md): digest-pinned offline runtime, immutable image verification, stable Capsule identity, monotonic control generations, fresh TLS/authentication, policy-controlled modes, quarantine, ownership checks, and secure reconnection of the same mutable VM. Advanced construction and reconnect operations remain in configuration and the Python API. The chat model cannot enable provider capabilities or expand execution policy.

Windows ISO-derived production control remains blocked while protected bootstrap-media delivery is not natively verified. Linux provisioning requires a real libvirt/KVM host. Unit tests and the desktop starter test do not establish native ISO provisioning acceptance. Manually prepared compatibility guests remain a separate path with their documented trust requirements.

Failure retention applies to supported scripted Capsule run failures. Roam findings do not currently trigger that retention hook. Use the provider and provisioning guides for operator requirements and recovery procedures.
