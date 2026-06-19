"""Project configuration: ``.argus/config.yaml`` + ``ARGUS_*`` env vars.

Example ``.argus/config.yaml``::

    provider: ollama            # active provider
    providers:
      ollama:
        model: gemma3:9b
        base_url: http://localhost:11434
      anthropic:
        model: claude-sonnet-4-6
        api_key_env: ANTHROPIC_API_KEY
      openai:
        model: gpt-4o
        api_key_env: OPENAI_API_KEY

    budgets:
      time_minutes: 10          # default time budget (all providers)
      max_tokens: null          # optional token budget (ignored for ollama)

    knowledge:
      enabled: true
      type: local               # local | docker | external
      embedding_model: all-MiniLM-L6-v2

Environment overrides: ``ARGUS_PROVIDER``, ``ARGUS_MODEL``, ``ARGUS_API_KEY``,
``ARGUS_BASE_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from argus.providers import LLMProvider, create_provider
from argus.tokens import Budget, TokenTracker

DEFAULT_CONFIG = """\
# Argus configuration
# Active provider: ollama | anthropic | openai | azure | gemini | litellm
provider: ollama

providers:
  ollama:
    model: gemma3:9b
    base_url: http://localhost:11434
  anthropic:
    model: claude-sonnet-4-6
    api_key_env: ANTHROPIC_API_KEY
  openai:
    model: gpt-4o
    api_key_env: OPENAI_API_KEY
  # azure:
  #   model: my-gpt4o-deployment
  #   base_url: https://YOUR-RESOURCE.openai.azure.com/openai/v1
  #   api_key_env: AZURE_OPENAI_API_KEY
  # gemini:
  #   model: gemini-2.0-flash
  #   api_key_env: GEMINI_API_KEY
  # litellm:
  #   model: anything
  #   base_url: http://localhost:4000/v1
  #   api_key_env: LITELLM_API_KEY

budgets:
  # Time budget in minutes (applies to every provider; the only budget
  # used for ollama, which is local and free).
  time_minutes: 10
  # Optional token budget for paid providers (null = no token cap).
  max_tokens: null

# Knowledge engine — persistent graph + vector learning store.
# Requires: pip install argus-app-testing[knowledge]
# knowledge:
#   enabled: true
#   type: local          # local | docker | external
#   vector_backend: chroma   # chroma (local) | qdrant (docker/external)
#   vector_url: null     # Qdrant URL; auto-set when type: docker
#   embedding_model: all-MiniLM-L6-v2
"""


@dataclass
class ProviderConfig:
    type: str
    model: str
    api_key: str = ""
    base_url: Optional[str] = None


@dataclass
class KnowledgeConfig:
    enabled: bool = True
    type: str = "auto"            # "auto" | "json" | "local" | "docker" | "external"
    vector_backend: str = "chroma"
    vector_url: Optional[str] = None
    persist_dir: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"


@dataclass
class ArgusConfig:
    project_dir: Path
    provider: ProviderConfig
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    time_minutes: Optional[float] = 10.0
    max_tokens: Optional[int] = None
    raw: dict = field(default_factory=dict)

    @property
    def argus_dir(self) -> Path:
        return self.project_dir / ".argus"

    def make_provider(self, tracker: Optional[TokenTracker] = None) -> LLMProvider:
        return create_provider(
            self.provider.type,
            model=self.provider.model,
            api_key=self.provider.api_key,
            base_url=self.provider.base_url,
            tracker=tracker,
        )

    def make_budget(
        self,
        tracker: TokenTracker,
        time_minutes: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Budget:
        """Build the session budget honoring the provider rules:

        * ollama  → time budget only (token caps are ignored)
        * others  → time and/or token budget, whichever the user set
        """
        minutes = time_minutes if time_minutes is not None else self.time_minutes
        tokens = max_tokens if max_tokens is not None else self.max_tokens
        if self.provider.type == "ollama":
            tokens = None
        return Budget(
            max_seconds=minutes * 60 if minutes else None,
            max_tokens=tokens,
            tracker=tracker,
        )

    def make_knowledge_store(self):
        """Return a KnowledgeStore (or None if disabled / unavailable)."""
        from argus.knowledge import create_knowledge_store
        kc = self.knowledge
        persist = Path(kc.persist_dir) if kc.persist_dir else self.argus_dir / "knowledge"
        return create_knowledge_store(
            enabled=kc.enabled,
            store_type=kc.type,
            vector_backend=kc.vector_backend,
            vector_url=kc.vector_url,
            persist_dir=persist,
            embedding_model=kc.embedding_model,
            data_dir=self.argus_dir,
        )


def _resolve_api_key(entry: dict) -> str:
    """api_key wins; otherwise read the env var named by api_key_env;
    ARGUS_API_KEY is the universal override."""
    if os.environ.get("ARGUS_API_KEY"):
        return os.environ["ARGUS_API_KEY"]
    if entry.get("api_key"):
        return str(entry["api_key"])
    env_name = entry.get("api_key_env")
    if env_name:
        return os.environ.get(env_name, "")
    return ""


def load_config(project_dir: Optional[Path] = None) -> ArgusConfig:
    project_dir = (project_dir or Path.cwd()).resolve()
    cfg_path = project_dir / ".argus" / "config.yaml"
    raw: dict = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    active = os.environ.get("ARGUS_PROVIDER") or raw.get("provider") or "ollama"
    providers = raw.get("providers") or {}
    entry = dict(providers.get(active) or {})

    model = os.environ.get("ARGUS_MODEL") or entry.get("model") or _default_model(active)
    base_url = os.environ.get("ARGUS_BASE_URL") or entry.get("base_url")

    budgets = raw.get("budgets") or {}
    time_minutes = budgets.get("time_minutes", 10)
    max_tokens = budgets.get("max_tokens")

    kc_raw = raw.get("knowledge") or {}
    knowledge = KnowledgeConfig(
        enabled=bool(kc_raw.get("enabled", True)),
        type=str(kc_raw.get("type", "local")),
        vector_backend=str(kc_raw.get("vector_backend", "chroma")),
        vector_url=kc_raw.get("vector_url") or None,
        persist_dir=kc_raw.get("persist_dir") or None,
        embedding_model=str(kc_raw.get("embedding_model", "all-MiniLM-L6-v2")),
    )

    return ArgusConfig(
        project_dir=project_dir,
        provider=ProviderConfig(
            type=active,
            model=str(model),
            api_key=_resolve_api_key(entry),
            base_url=base_url,
        ),
        knowledge=knowledge,
        time_minutes=float(time_minutes) if time_minutes else None,
        max_tokens=int(max_tokens) if max_tokens else None,
        raw=raw,
    )


def _default_model(provider_type: str) -> str:
    return {
        "ollama": "gemma3:9b",
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o",
        "gemini": "gemini-2.0-flash",
    }.get(provider_type, "gemma3:9b")


def init_project(project_dir: Optional[Path] = None) -> Path:
    """Create ``.argus/`` with a starter config and example test. Idempotent."""
    project_dir = (project_dir or Path.cwd()).resolve()
    argus_dir = project_dir / ".argus"
    argus_dir.mkdir(parents=True, exist_ok=True)
    (argus_dir / "runs").mkdir(exist_ok=True)
    (argus_dir / "roam").mkdir(exist_ok=True)

    cfg = argus_dir / "config.yaml"
    if not cfg.exists():
        cfg.write_text(DEFAULT_CONFIG, encoding="utf-8")

    example = argus_dir / "notepad.test.yaml"
    if not example.exists():
        example.write_text(EXAMPLE_TEST, encoding="utf-8")
    return argus_dir


EXAMPLE_TEST = """\
# Example Argus test — Windows Notepad
name: Notepad types and finds text
target:
  adapter: desktop-gui
  launch: notepad.exe

steps:
  - "Type the sentence 'hello from argus' into the editor"
  - assert:
      text_visible: "hello from argus"
  - "Open the File menu"
  - assert:
      element_exists:
        name: "Save"
        control_type: MenuItem

teardown:
  - close
"""
