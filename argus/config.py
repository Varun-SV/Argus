"""Project configuration: ``.argus/config.yaml`` + ``ARGUS_*`` env vars.

Execution is local by default. Disposable Capsule execution can use secure
Hyper-V on Windows or libvirt/QEMU on Linux once an appropriate golden image
and in-guest Argus agent are configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

import yaml

from argus.providers import LLMProvider, create_provider
from argus.tokens import Budget, TokenTracker

CAPSULE_GUEST_TOKEN_ENV = "ARGUS_CAPSULE_GUEST_TOKEN"
SESSION_CAPSULE_OVERRIDES = frozenset({"provider", "retain_on_failure"})

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
  #   provider: hyperv          # hyperv | libvirt | auto
  #   guest_os: auto            # auto | windows | linux
  #   image: C:\\Argus\\images\\windows-11-clean.vhdx
  #   switch_name: Default Switch   # Hyper-V Internal switch
  #   vm_root: C:\\Argus\\capsules
  #   memory_mb: 4096
  #   cpu_count: 2
  #   guest_port: 8765
  #   guest_token_ref: secret://argus/capsule/bootstrap # optional per-user store reference
  #   guest_input_mode: physical
  #   guest_address: null
  #   boot_timeout_seconds: 120
  #   agent_timeout_seconds: 60
  #   retain_on_failure: false
  #
  #   # Linux libvirt/QEMU (provider: libvirt or provider: auto on Linux)
  #   # image: /var/lib/libvirt/images/argus-linux-golden.qcow2
  #   # vm_root: /var/lib/libvirt/images/argus-capsules
  #   libvirt_uri: qemu:///system
  #   libvirt_network_cidr: ""   # optional site-specific private /24, e.g. 10.250.77.0/24
  #   libvirt_arch: ""           # auto host arch; x86_64 or aarch64 when pinned
  #   libvirt_machine: ""        # optional libvirt/QEMU machine type
  #   libvirt_qemu_group: ""     # trusted system QEMU group for provisioned media
  #
  #   # PR6 secure control plane / network isolation
  #   guest_transport: https
  #   guest_ca_cert: C:\\Argus\\certs\\argus-guest-ca.pem
  #   allow_insecure_http: false   # explicit legacy-only escape hatch
  #   rotate_session_token: true   # bootstrap token -> random per-session bearer
  #   network_mode: host_only      # host_only | allowlist (allowlist currently Hyper-V only)
  #   egress_allowlist: []         # CIDRs, e.g. ["10.20.30.0/24"]
  #   allow_dhcp: true
  #   disable_guest_file_copy: true
  #   allow_external_switch: false # External/bridged networking remains unsupported

# Knowledge engine — persistent graph + vector learning store.
# Requires: pip install argus-app-testing[knowledge]
# knowledge:
#   enabled: true
#   type: local
#   vector_backend: chroma
#   vector_url: null
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
    environment_definition: str = ""
    image_cache_root: str = ""
    provisioning_evidence_root: str = ""
    image_format: str = ""
    control_root: str = ""
    default_execution_mode: str = "isolated"
    allowed_execution_modes: tuple[str, ...] = ("isolated",)
    allow_llm_mode_change: bool = False
    failure_allow_reconnect: bool = True
    failure_allow_llm_reconnect: bool = False
    provider: str = "hyperv"
    guest_os: str = "auto"
    image: str = ""
    switch_name: str = ""
    vm_root: str = ""
    memory_mb: int = 4096
    cpu_count: int = 2
    guest_port: int = 8765
    guest_token_env: str = CAPSULE_GUEST_TOKEN_ENV
    guest_token_ref: str = ""
    guest_input_mode: str = "physical"
    guest_address: str = ""
    boot_timeout_seconds: float = 120.0
    agent_timeout_seconds: float = 60.0
    allow_external_switch: bool = False
    retain_on_failure: bool = False
    guest_transport: str = "https"
    guest_ca_cert: str = ""
    allow_insecure_http: bool = False
    rotate_session_token: bool = True
    network_mode: str = "host_only"
    egress_allowlist: tuple[str, ...] = ()
    allow_dhcp: bool = True
    disable_guest_file_copy: bool = True
    libvirt_uri: str = "qemu:///system"
    libvirt_network_cidr: str = ""
    libvirt_arch: str = ""
    libvirt_machine: str = ""
    libvirt_qemu_group: str = ""


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
        capsule_overrides: Optional[Mapping[str, object]] = None,
    ):
        """Build the configured local or Capsule execution environment.

        A provisioned environment verifies cached image bytes and ATES evidence
        before binding Capsule settings. Static credentials are only read for
        the legacy image path. Model requests cannot expand host mode policy.
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
        if cc.environment_definition:
            guest_token = ""
        elif cc.guest_token_ref:
            from argus.secrets import ArgusSecretStore

            guest_token = ArgusSecretStore().get(cc.guest_token_ref)
        else:
            guest_token = os.environ.get(CAPSULE_GUEST_TOKEN_ENV, "")
        capsule_config = {
            "control_root": cc.control_root,
            "default_execution_mode": cc.default_execution_mode,
            "allowed_execution_modes": cc.allowed_execution_modes,
            "allow_llm_mode_change": cc.allow_llm_mode_change,
            "failure_allow_reconnect": cc.failure_allow_reconnect,
            "failure_allow_llm_reconnect": cc.failure_allow_llm_reconnect,
            "provider": os.environ.get("ARGUS_CAPSULE_PROVIDER") or cc.provider,
            "guest_os": os.environ.get("ARGUS_CAPSULE_GUEST_OS") or cc.guest_os,
            "image": os.environ.get("ARGUS_CAPSULE_IMAGE") or cc.image,
            "switch_name": os.environ.get("ARGUS_CAPSULE_SWITCH") or cc.switch_name,
            "vm_root": os.environ.get("ARGUS_CAPSULE_VM_ROOT") or cc.vm_root,
            "memory_mb": _env_int("ARGUS_CAPSULE_MEMORY_MB", cc.memory_mb),
            "cpu_count": _env_int("ARGUS_CAPSULE_CPU_COUNT", cc.cpu_count),
            "guest_port": _env_int("ARGUS_CAPSULE_GUEST_PORT", cc.guest_port),
            "guest_token": guest_token,
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
            "guest_transport": (
                os.environ.get("ARGUS_CAPSULE_GUEST_TRANSPORT") or cc.guest_transport
            ),
            "guest_ca_cert": (
                os.environ.get("ARGUS_CAPSULE_GUEST_CA_CERT") or cc.guest_ca_cert
            ),
            "allow_insecure_http": _env_bool(
                "ARGUS_CAPSULE_ALLOW_INSECURE_HTTP", cc.allow_insecure_http
            ),
            "rotate_session_token": _env_bool(
                "ARGUS_CAPSULE_ROTATE_SESSION_TOKEN", cc.rotate_session_token
            ),
            "network_mode": os.environ.get("ARGUS_CAPSULE_NETWORK_MODE") or cc.network_mode,
            "egress_allowlist": _env_cidrs(
                "ARGUS_CAPSULE_EGRESS_ALLOWLIST", cc.egress_allowlist
            ),
            "allow_dhcp": _env_bool("ARGUS_CAPSULE_ALLOW_DHCP", cc.allow_dhcp),
            "disable_guest_file_copy": _env_bool(
                "ARGUS_CAPSULE_DISABLE_GUEST_FILE_COPY", cc.disable_guest_file_copy
            ),
            "libvirt_uri": os.environ.get("ARGUS_CAPSULE_LIBVIRT_URI") or cc.libvirt_uri,
            "libvirt_network_cidr": (
                os.environ.get("ARGUS_CAPSULE_LIBVIRT_NETWORK_CIDR")
                or cc.libvirt_network_cidr
            ),
            "libvirt_arch": (
                os.environ.get("ARGUS_CAPSULE_LIBVIRT_ARCH") or cc.libvirt_arch
            ),
            "libvirt_machine": (
                os.environ.get("ARGUS_CAPSULE_LIBVIRT_MACHINE") or cc.libvirt_machine
            ),
            "libvirt_qemu_group": (
                os.environ.get("ARGUS_CAPSULE_LIBVIRT_QEMU_GROUP") or cc.libvirt_qemu_group
            ),
        }
        for key, value in (capsule_overrides or {}).items():
            if key not in SESSION_CAPSULE_OVERRIDES:
                raise ValueError(f"Capsule setting {key!r} cannot be overridden per session")
            if key == "retain_on_failure":
                value = _strict_bool(value, "retain_on_failure")
            else:
                value = str(value).lower().strip()
                if value not in {"hyperv", "libvirt", "auto"}:
                    raise ValueError("Capsule provider must be hyperv, libvirt or auto")
            capsule_config[key] = value
        if cc.environment_definition:
            from dataclasses import asdict
            from argus.capsule.base import CapsuleSettings
            from argus.provisioning.build import load_published_derived_image
            from argus.provisioning.capsule_bridge import capsule_settings_from_derived_image
            from argus.provisioning.planner import build_provisioning_plan
            from argus.provisioning.providers import HyperVProvisioner, LibvirtProvisioner
            from argus.provisioning.spec import load_environment_definition

            if not cc.image_cache_root:
                raise ValueError("provisioned Capsule requires image_cache_root")
            if capsule_config["image"]:
                raise ValueError("provisioned Capsule selects its image from the verified cache")
            def project_path(value):
                path = Path(value).expanduser()
                return path if path.is_absolute() else self.project_dir / path

            definition = load_environment_definition(project_path(cc.environment_definition))
            if str(capsule_config["provider"]).lower() == "auto":
                import platform

                capsule_config["provider"] = {"windows": "hyperv", "linux": "libvirt"}.get(
                    platform.system().lower(), "unsupported")
            provider = {"hyperv": HyperVProvisioner, "libvirt": LibvirtProvisioner}.get(
                str(capsule_config["provider"]).lower())
            if provider is None:
                raise ValueError("unsupported provisioned Capsule provider")
            image_format = cc.image_format or ("vhdx" if provider is HyperVProvisioner else "qcow2")
            provisioner = (provider(switch_name=str(capsule_config["switch_name"]))
                           if provider is HyperVProvisioner else provider(network_name="argus-build"))
            plan = build_provisioning_plan(
                definition, provisioner.capabilities(), output_format=image_format,
                cache_root=project_path(cc.image_cache_root),
                evidence_root=(project_path(cc.provisioning_evidence_root)
                               if cc.provisioning_evidence_root else None),
            )
            published = load_published_derived_image(definition, plan)
            capsule_config = asdict(capsule_settings_from_derived_image(
                definition, published.manifest, plan.image_path,
                settings=CapsuleSettings(**capsule_config),
            ))
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


def _env_cidrs(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.environ.get(name)
    if value is None:
        return tuple(default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


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

    raw_allowlist = capsule_raw.get("egress_allowlist") or []
    if not isinstance(raw_allowlist, list):
        raise ValueError("execution.capsule.egress_allowlist must be a list of CIDRs")
    raw_modes = capsule_raw.get("allowed_execution_modes", ["isolated"])
    if not isinstance(raw_modes, list) or not raw_modes or any(
        mode not in {"isolated", "shared_user"} for mode in raw_modes
    ):
        raise ValueError("execution.capsule.allowed_execution_modes must list supported modes")

    execution = ExecutionConfig(
        environment=str(execution_raw.get("environment") or "local"),
        capsule=CapsuleConfig(
            environment_definition=str(capsule_raw.get("environment_definition") or ""),
            image_cache_root=str(capsule_raw.get("image_cache_root") or ""),
            provisioning_evidence_root=str(capsule_raw.get("provisioning_evidence_root") or ""),
            image_format=str(capsule_raw.get("image_format") or ""),
            control_root=str(capsule_raw.get("control_root") or ""),
            default_execution_mode=str(capsule_raw.get("default_execution_mode") or "isolated"),
            allowed_execution_modes=tuple(raw_modes),
            allow_llm_mode_change=_strict_bool(capsule_raw.get("allow_llm_mode_change", False),
                                             "execution.capsule.allow_llm_mode_change"),
            failure_allow_reconnect=_strict_bool(capsule_raw.get("failure_allow_reconnect", True),
                                                "execution.capsule.failure_allow_reconnect"),
            failure_allow_llm_reconnect=_strict_bool(capsule_raw.get("failure_allow_llm_reconnect", False),
                                                    "execution.capsule.failure_allow_llm_reconnect"),
            provider=str(capsule_raw.get("provider") or "hyperv"),
            guest_os=str(capsule_raw.get("guest_os") or "auto"),
            image=str(capsule_raw.get("image") or ""),
            switch_name=str(capsule_raw.get("switch_name") or ""),
            vm_root=str(capsule_raw.get("vm_root") or ""),
            memory_mb=int(capsule_raw.get("memory_mb") or 4096),
            cpu_count=int(capsule_raw.get("cpu_count") or 2),
            guest_port=int(capsule_raw.get("guest_port") or 8765),
            guest_token_env=CAPSULE_GUEST_TOKEN_ENV,
            guest_token_ref=str(capsule_raw.get("guest_token_ref") or ""),
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
            guest_transport=str(capsule_raw.get("guest_transport") or "https"),
            guest_ca_cert=str(capsule_raw.get("guest_ca_cert") or ""),
            allow_insecure_http=_strict_bool(
                capsule_raw.get("allow_insecure_http", False),
                "execution.capsule.allow_insecure_http",
            ),
            rotate_session_token=_strict_bool(
                capsule_raw.get("rotate_session_token", True),
                "execution.capsule.rotate_session_token",
            ),
            network_mode=str(capsule_raw.get("network_mode") or "host_only"),
            egress_allowlist=tuple(str(item).strip() for item in raw_allowlist),
            allow_dhcp=_strict_bool(
                capsule_raw.get("allow_dhcp", True),
                "execution.capsule.allow_dhcp",
            ),
            disable_guest_file_copy=_strict_bool(
                capsule_raw.get("disable_guest_file_copy", True),
                "execution.capsule.disable_guest_file_copy",
            ),
            libvirt_uri=str(capsule_raw.get("libvirt_uri") or "qemu:///system"),
            libvirt_network_cidr=str(capsule_raw.get("libvirt_network_cidr") or ""),
            libvirt_arch=str(capsule_raw.get("libvirt_arch") or ""),
            libvirt_machine=str(capsule_raw.get("libvirt_machine") or ""),
            libvirt_qemu_group=str(capsule_raw.get("libvirt_qemu_group") or ""),
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
