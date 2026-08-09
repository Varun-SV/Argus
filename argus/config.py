"""Project configuration: ``.argus/config.yaml`` + ``ARGUS_*`` env vars.

Execution is local by default. A Windows Hyper-V Capsule can be selected with
``execution.environment: capsule`` once a golden VHDX and in-guest Argus agent
are configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from argus.providers import LLMProvider, create_provider
from argus.tokens import Budget, TokenTracker

CAPSULE_GUEST_TOKEN_ENV = "ARGUS_CAPSULE_GUEST_TOKEN"

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
  time_minutes: 10
  max_tokens: null

# Execution location. "local" preserves normal host execution.
execution:
  environment: local            # local | capsule
  # capsule:
  #   provider: hyperv
  #   image: C:\\Argus\\images\\windows-11-clean.vhdx
  #   switch_name: Default Switch   # must be an Internal Hyper-V switch in PR3
  #   vm_root: C:\\Argus\\capsules
  #   memory_mb: 4096
  #   cpu_count: 2
  #   guest_port: 8765
  #   guest_input_mode: physical    # physical input is contained inside the VM
  #   guest_address: null           # optional candidate; Hyper-V must attest it belongs to the VM
  #   boot_timeout_seconds: 120
  #   agent_timeout_seconds: 60
  #   allow_external_switch: false  # reserved; true is rejected until transport is confidential
  #   retain_on_failure: false      # save failed VM state instead of deleting the Capsule

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
    type: str = "auto"
    vector_backend: str = "chroma"
    vector_url: Optional[str] = None
    persist_dir: Optional[str] = None
    embedding_model: str = "all-MiniLM-L6-v2"


@dataclass
class CapsuleConfig:
    provider: str = "hyperv"
    image: str = ""
    switch_name: str = ""
    vm_root: str = ""
    memory_mb: int = 4096
    cpu_count: int = 2
    guest_port: int = 8765
    guest_token_env: str = CAPSULE_GUEST_TOKEN_ENV
    guest_input_mode: str = "physical"
    guest_address: str = ""
    boot_timeout_seconds: float = 120.0
    agent_timeout_seconds: float = 60.0
    allow_external_switch: bool = False
    retain_on_failure: bool = False


@dataclass
class ExecutionConfig:
    environment: str = "local"
    capsule: CapsuleConfig = field(default_factory=CapsuleConfig)


@dataclass
class ArgusConfig:
    project_dir: Path
    provider: ProviderConfig
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
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

    def make_execution_environment(
        self,
        adapter_type: str,
        environment_type: Optional[str] = None,
    ):
        """Build the configured local or Capsule execution environment.

        Capsule control credentials always come from the dedicated host variable
        ``ARGUS_CAPSULE_GUEST_TOKEN``. Project configuration is intentionally not
        allowed to select an arbitrary host environment variable.
        """
        from argus.execution import create_execution_environment

        kind = (
            os.environ.get("ARGUS_EXECUTION_ENVIRONMENT")
            or environment_type
            or self.execution.environment
            or "local"
        )
        if str(kind).lower().strip() != "capsule":
            return create_execution_environment(adapter_type, environment_type=str(kind))

        cc = self.execution.capsule
        if cc.guest_token_env != CAPSULE_GUEST_TOKEN_ENV:
            raise ValueError(
                "execution.capsule.guest_token_env cannot select a host secret; "
                f"Capsule credentials are read only from {CAPSULE_GUEST_TOKEN_ENV}"
            )
        capsule_config = {
            "provider": os.environ.get("ARGUS_CAPSULE_PROVIDER") or cc.provider,
            "image": os.environ.get("ARGUS_CAPSULE_IMAGE") or cc.image,
            "switch_name": os.environ.get("ARGUS_CAPSULE_SWITCH") or cc.switch_name,
            "vm_root": os.environ.get("ARGUS_CAPSULE_VM_ROOT") or cc.vm_root,
            "memory_mb": _env_int("ARGUS_CAPSULE_MEMORY_MB", cc.memory_mb),
            "cpu_count": _env_int("ARGUS_CAPSULE_CPU_COUNT", cc.cpu_count),
            "guest_port": _env_int("ARGUS_CAPSULE_GUEST_PORT", cc.guest_port),
            "guest_token": os.environ.get(CAPSULE_GUEST_TOKEN_ENV, ""),
            "guest_input_mode": (
                os.environ.get("ARGUS_CAPSULE_GUEST_INPUT_MODE") or cc.guest_input_mode
            ),
            "guest_address": os.environ.get("ARGUS_CAPSULE_GUEST_ADDRESS") or cc.guest_address,
            "boot_timeout_seconds": _env_float(
                "ARGUS_CAPSULE_BOOT_TIMEOUT_SECONDS", cc.boot_timeout_seconds
            ),
            "agent_timeout_seconds": _env_float(
                "ARGUS_CAPSULE_AGENT_TIMEOUT_SECONDS", cc.agent_timeout_seconds
            ),
            "allow_external_switch": _env_bool(
                "ARGUS_CAPSULE_ALLOW_EXTERNAL_SWITCH", cc.allow_external_switch
            ),
            "retain_on_failure": _env_bool(
                "ARGUS_CAPSULE_RETAIN_ON_FAILURE", cc.retain_on_failure
            ),
        }
        return create_execution_environment(
            adapter_type,
            environment_type="capsule",
            capsule_config=capsule_config,
        )

    def make_budget(
        self,
        tracker: TokenTracker,
        time_minutes: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Budget:
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
    if os.environ.get("ARGUS_API_KEY"):
        return os.environ["ARGUS_API_KEY"]
    if entry.get("api_key"):
        return str(entry["api_key"])
    env_name = entry.get("api_key_env")
    if env_name:
        return os.environ.get(env_name, "")
    return ""


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in {None, ""} else int(default)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in {None, ""} else float(default)


def _strict_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be a boolean (true/false)")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return _strict_bool(default, name)
    return _strict_bool(value, name)


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

    execution_raw = raw.get("execution") or {}
    capsule_raw = execution_raw.get("capsule") or {}
    configured_token_env = str(
        capsule_raw.get("guest_token_env") or CAPSULE_GUEST_TOKEN_ENV
    )
    if configured_token_env != CAPSULE_GUEST_TOKEN_ENV:
        raise ValueError(
            "execution.capsule.guest_token_env cannot select arbitrary host "
            f"environment variables; only {CAPSULE_GUEST_TOKEN_ENV} is allowed"
        )
    execution = ExecutionConfig(
        environment=str(execution_raw.get("environment") or "local"),
        capsule=CapsuleConfig(
            provider=str(capsule_raw.get("provider") or "hyperv"),
            image=str(capsule_raw.get("image") or ""),
            switch_name=str(capsule_raw.get("switch_name") or ""),
            vm_root=str(capsule_raw.get("vm_root") or ""),
            memory_mb=int(capsule_raw.get("memory_mb") or 4096),
            cpu_count=int(capsule_raw.get("cpu_count") or 2),
            guest_port=int(capsule_raw.get("guest_port") or 8765),
            guest_token_env=CAPSULE_GUEST_TOKEN_ENV,
            guest_input_mode=str(capsule_raw.get("guest_input_mode") or "physical"),
            guest_address=str(capsule_raw.get("guest_address") or ""),
            boot_timeout_seconds=float(
                capsule_raw.get("boot_timeout_seconds") or 120.0
            ),
            agent_timeout_seconds=float(
                capsule_raw.get("agent_timeout_seconds") or 60.0
            ),
            allow_external_switch=_strict_bool(
                capsule_raw.get("allow_external_switch", False),
                "execution.capsule.allow_external_switch",
            ),
            retain_on_failure=_strict_bool(
                capsule_raw.get("retain_on_failure", False),
                "execution.capsule.retain_on_failure",
            ),
        ),
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
        execution=execution,
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
