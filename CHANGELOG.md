# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- Desktop app redesigned as a chat with Argus (Claude-style design): warm ivory layout, sidebar with tests and recent conversations, result cards for runs, roams, drafted specs, dry runs, providers, tokens, history, knowledge, ATES evidence, execution environment and watch mode, and a live-view panel with the latest screenshot
- Free-text messages in the desktop app are routed through the configured provider to a fixed, validated intent set; slash commands (`/run`, `/roam`, `/write`, `/evidence`, …) work without a model
- Draft `.test.yaml` specs from plain English (validated with the spec parser, never saved without confirmation), explain failed runs, and turn roam findings into regression tests from the desktop app
- Per-session provider, Local/Capsule environment, Failure Capsule retention and roam memory pickers in the desktop app (`.argus/config.yaml` is not modified)
- Bundled SIL OFL fonts for the desktop app (Newsreader, IBM Plex Sans, IBM Plex Mono)
- Redesigned project website (`index.html`) in the same design language
### Fixed
- Desktop app no longer double-counts project token usage when several runs happen in one session
- Hybrid knowledge engine (`argus/knowledge/`) — persistent graph + vector store that accumulates learning across sessions
- `LocalKnowledgeStore`: ChromaDB (vectors) + NetworkX (state graph), zero-config, disk-backed at `.argus/knowledge/`
- `RemoteKnowledgeStore`: Qdrant vector DB + NetworkX graph for higher-throughput workloads
- `DockerManager`: self-managed `argus-qdrant` container lifecycle (pull, start, health-poll, reuse)
- State fingerprinting via SHA-256 of window title + element structure → stable 16-char state IDs
- Semantic embeddings with `sentence-transformers` (`all-MiniLM-L6-v2`) for similarity retrieval
- `KnowledgeContext` injected into LLM prompt — similar past states, past bugs, unexplored path hints
- Knowledge integration in roam mode: record state/transition/finding, inject context before each LLM call
- Knowledge integration in run mode: record state/assertion failures, finalize session on completion
- `argus knowledge` CLI command group: `stats`, `reset`, `export`, `docker up|down|status`
- `KnowledgeConfig` dataclass in `argus/config.py` with YAML parsing support
- Knowledge tab in desktop GUI showing per-target stats (states, transitions, bugs, sessions)
- `knowledge` optional dependency group: `chromadb>=0.5`, `networkx>=3.2`, `sentence-transformers>=2.7`
- `knowledge-remote` optional dependency group: `qdrant-client>=1.9`
- Updated marketing `index.html`: "Adaptive Learning Engine" feature card + dedicated Knowledge Engine section
- `CHANGELOG.md` in Keep-a-Changelog format
- Cross-platform desktop packaging: Windows x64/ARM64 portable ZIP, setup EXE, and MSI; macOS universal2 app/DMG; Linux x86_64/ARM64 AppImage, DEB, RPM, and Arch packages
- Native CI coverage across Windows, Linux, macOS, x64, and ARM64 where GitHub-hosted runners support it
- Reusable release-artifact workflow shared by PR package previews and production releases
- Automatic release-on-merge with tag-derived semantic versions, dynamic README/site release status, GitHub Release assets, checksums, provenance attestations, PyPI publishing, and explicit Pages deployment without bypassing protected `main`
- Reproducible existing-tag release workflow for rebuilding immutable tagged source
- CodeQL, dependency review, Dependabot, CODEOWNERS, contribution/security policies, and structured PR/issue templates
- `setuptools-scm` runtime/package versioning plus `.github/scripts/next_release.py` for deterministic tag calculation

## [0.1.0] - 2024-01-01
### Added
- Initial release with NL test execution, structured assertions, and free-roam exploration mode
- Observe → Think → Act agent loop with multimodal LLM support
- Support for Anthropic, OpenAI, Azure OpenAI, Gemini, Ollama, and LiteLLM providers
- Adapters for Windows GUI (UIA tree + input synthesis), Linux GUI (X11/Xvfb), browser (Playwright), and CLI
- YAML-based test specification with NL steps, assertions, and retries
- Token tracking, cost estimation, and configurable token/time budgets
- Web dashboard (`argus serve`) and native desktop GUI (`argus gui`) via pywebview
- File-watch mode (`argus run --watch`) for re-running tests on YAML change
- `argus init` project scaffolding with example test files
