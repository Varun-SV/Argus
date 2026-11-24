# Desktop app

The desktop app uses the same runner, adapters, execution environments, knowledge store, and ATES authority as the CLI. Use **Tests** to build and run checks, **Roam** to explore a target, and **Run history** to review results and verify evidence. Chat and the live view show progress during execution.

## Open a project and run the first test

Install a desktop package from [GitHub Releases](https://github.com/Varun-SV/Argus/releases/latest), or install the Python GUI extra and run:

```bash
pip install "argus-app-testing[gui]"
argus gui
```

The desktop installers include the app and its supported adapters. Use **Settings → Runtime** to install or check Chromium. Python installations need the [target adapter dependencies](../README.md#python-package) as well as a pywebview backend. Packaged Linux apps include Qt WebEngine.

1. Click **Open project folder**, beside the project name at the bottom of the sidebar. A folder opens in a separate window with its own jobs and session settings. Launching from a folder containing `.argus/` opens that project directly.
2. For a new folder, choose **Set up this project**. Argus creates the project settings and a sample test while leaving existing files in place.
3. Click **Run smoke test**. Argus starts the platform's echo command and checks its output and exit code without calling a model.
4. Open **Evidence** on the completed result to verify the canonical ATES manifest. Use **Run history** to inspect persisted results.

Project initialization starts after you select a folder. If settings are damaged, open **Settings → Restore project defaults** to back them up and recover inside Argus.

One desktop window owns each project's conversation history. Opening that project again focuses its existing window; a second Argus process asks you to use or close the owning window. Different projects can be open together.

## Configure a model

In **Settings → Model**, choose a provider and enter the model or deployment ID. Add the endpoint and API key where required. Argus stores entered keys in its per-user secret store. Save the settings and choose **Check saved model** to verify the connection and vision support before running model-driven actions.

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

Name the tests you want to run explicitly. Exclusions or conditions such as "run all except destructive" need clarification before anything executes. Questions about what an action does are informational; direct requests such as "Can you run smoke?" authorize it. `/roam` requires valid option values: `--minutes` must be finite, greater than zero, and at most 240; conflicting options are rejected.

Negative requests such as "no need to run checkout" do not authorize execution. Alternatives such as "run either safe or destructive" and uncertain requests such as "maybe run safe" ask for clarification. Use `and` or `then` for an explicit list, and quote literal test names containing command words. Test drafting passes your original request, including URLs, flags and constraints, to the generator.

Temporal requests such as "run checkout after deployment" need clarification too. Advice requests such as "Tell me how to stop" do not perform the action. Test matching excludes command and environment wording; use an exact filename or quoted name when a name is ambiguous. `/roam git status`, `/roam npm test`, and `/roam python` select the CLI adapter; an explicit `--adapter` remains authoritative.

Choose **New test** to set the launch target and add steps and checks. To work from a generated draft, choose **Review draft**, fill in the expected results, and save it from the same editor. A draft saves as a new test. To update an existing test, open it from **Tests** and use **Edit**. If the file changed while you were editing, reopen it to load the latest version.

**Stop** requests cancellation and lets execution perform its normal teardown. Closing a window with an active job requests Stop and keeps the window open until cleanup finishes; close it again after the result settles. Watch mode observes `.argus/*.test.yaml` and re-runs changed files. Stop watching before editing tests you do not want to execute.

A late Stop click does not replace a completed Roam outcome; cancellation is reported only when the engine records it. Roam durations must be positive, finite and at most 240 minutes; larger requests are rejected instead of silently shortened. Omitting a duration uses the project default within that range, while duration-like text inside a quoted command stays part of that command.

Broader refusals such as "no need to initialize", "you should not stop" and "I would rather not save" do not authorize those actions. Draft descriptions may still contain quoted titles and negative assertions. Ordered repeated tests keep separate result directories, reports and history entries even when they execute within one second; re-saving one result updates its own entry, and older history remains readable.

Watch cards retain the latest 200 completed events and all pending changes. The default knowledge store rejects redirected project directories, including Windows junctions. Explicitly configured external persistence directories remain supported. Switching back to the project's default provider restores its generic `ARGUS_API_KEY`, `ARGUS_MODEL`, and `ARGUS_BASE_URL` settings; another provider uses its own configured settings.

Watch captures its starting file versions before reporting that it is active, so edits immediately after acknowledgement remain observable.

Managed Docker knowledge uses a separate project-owned Qdrant container and an inspected, dynamically allocated localhost port. Each container requires its own generated API key, kept in Docker's configuration and passed privately to the client. Existing store handles recheck container ownership before remote operations; the credential also prevents a different Qdrant container from accepting a request if the port changes hands during that operation. Keys are not put in project files or Docker command arguments. `argus knowledge docker up`, `status` and `down` select the same project-owned Qdrant resource. An explicitly configured `knowledge.vector_url` with `knowledge.type: external` remains an operator-selected service; its isolation is the operator's responsibility. An explicit graph `knowledge.persist_dir` does not change the managed service's project storage authority.

The old global `argus-qdrant` container is not adopted or stopped automatically. If it is still writing this project's storage, inspect its mount and stop it yourself before starting the new container; preserve the data directory. If a container's ownership or storage cannot be verified, Argus reports guidance and the knowledge factory falls back to JSON. For running Linux containers created through `/proc/PID/fd/FD`, Argus checks the mounted directory's actual filesystem identity with container `stat`, so the creating process's exit or descriptor reuse is not proof of ownership. If this check is unavailable, or a stopped container's old source is gone, operator-verified container removal and recreation may be required against the retained data.

Inspecting default knowledge in a fresh folder asks you to set up the project first. Resetting knowledge while the store is disabled reports an error. If a named test disappears before a run starts, Argus rejects the entire requested list so you can refresh and choose again. Malformed history records are skipped while valid results remain available. A rejected roam launch is an error, and a report path appears only after that report exists.

The Knowledge card describes the actual store, including JSON fallback from optional backends. JSON uses keyword retrieval and has no vector backend. Chroma/Qdrant and embedding labels distinguish unverified initialization from known unavailability. Constructing these labels does not initialize clients, download models, or perform connectivity checks; an initialized Qdrant client alone does not establish service availability.

On macOS and other hosts without secure descriptor-backed directory paths, JSON is the default project knowledge backend when no type is configured. Explicit default Chroma/Docker selection stops with configuration guidance on those hosts; use `knowledge.type: json`, or an explicitly configured, operator-approved `knowledge.persist_dir` for the existing backend behavior. Full native macOS anchoring for these optional backends remains future work.

Conversations are stored in per-user application data and separated by project. A window reload reconnects to jobs and watch mode still owned by the current backend. After a process restart, an interrupted job is shown as **Outcome unknown**; check persisted history and ATES evidence before retrying. Malformed saved cards show a recovery message instead of preventing startup. Closing flushes the latest conversation and waits for a successful save. A failed or uncertain save keeps the window open for recovery, even when model configuration is invalid. Watch cards continue updating in their original conversation while you use another chat.

## Capsule readiness

The environment picker selects Local or Capsule execution for the session. Local execution shares the host; Capsule execution requires a configured provider, supported virtualization host, and prepared image. Selecting Capsule never silently falls back to Local.

`ARGUS_EXECUTION_ENVIRONMENT` accepts only `local` or `capsule`. Other values show a configuration error and block execution; correct the value and restart the app.

The effective project and session environment accepts `local` or `capsule`. To fix a project default, choose **Settings → Project** and save the execution location. An explicit host policy takes precedence.

`/env` distinguishes manually prepared images from verified ISO-derived cache entries and reports configuration blockers. This inspection does not allocate a VM or contact a guest. VM and guest readiness are checked when a run starts.

Capsule sessions accept only `auto`, `hyperv`, or `libvirt` as the effective provider. Invalid selections are rejected before jobs start; choose a valid session provider or correct the project/environment setting. Local execution remains usable when an unused Capsule provider setting is invalid.

Use **Settings → Capsules** to select a prepared disk, verify a published image, or build from an approved ISO. Building requires the matching hypervisor, host-only network, approved ISO and runtime digests, and an offline guest runtime bundle. Once the image and evidence are verified, select it and choose **Capsule** under **Settings → Project**. See the [OS provisioning guide](os-environment-provisioning.md) for host and runtime requirements.

Windows ISO-derived production control remains blocked while protected bootstrap-media delivery is not natively verified. Linux provisioning requires a real libvirt/KVM host. Unit tests and the desktop starter test do not establish native ISO provisioning acceptance. Manually prepared compatibility guests remain a separate path with their documented trust requirements.

Enable **Retain a Failure Capsule** in the project settings for supported scripted Capsule runs. From a retained run, **Inspect Capsule** uses the matching verified image and project control registry to reconnect to the same VM, capture one observation, and return it to quarantine.
