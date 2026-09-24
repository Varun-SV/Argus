# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
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
