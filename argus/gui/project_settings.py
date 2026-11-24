"""Desktop forms for the shared project configuration."""

from __future__ import annotations

import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from argus.config import _default_model, _resolve_api_key, config_from_mapping
from argus.providers.registry import PROVIDER_TYPES

PROVIDER_NAMES = {
    "ollama": "Ollama", "anthropic": "Anthropic", "openai": "OpenAI",
    "azure": "Azure OpenAI", "gemini": "Gemini", "litellm": "LiteLLM / compatible API",
}
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY", "litellm": "LITELLM_API_KEY",
}
_CAPSULE_PATHS = ("image", "guest_ca_cert", "vm_root", "environment_definition",
                  "image_cache_root", "provisioning_evidence_root")


def mapping_section(parent: dict, key: str) -> dict:
    entry = parent.get(key)
    if entry is None:
        entry = parent[key] = {}
    if not isinstance(entry, dict):
        raise ValueError("Open Settings to recover the saved project settings.")
    return entry


def endpoint_url(value: str, *, allow_http: bool = False) -> str:
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Enter a valid endpoint URL and port.") from None
    if (url.scheme not in ({"http", "https"} if allow_http else {"https"})
            and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Enter an HTTPS endpoint or an HTTP loopback endpoint.")
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Enter an endpoint without credentials, a query, or a fragment.")
    if port == 0:
        raise ValueError("Enter a valid endpoint port.")
    return value.rstrip("/")


def settings_view(project: Path, raw: dict) -> dict:
    providers = raw.get("providers") or {}
    default = raw.get("provider") or "ollama"
    rows = []
    for name in PROVIDER_TYPES:
        entry = providers.get(name) or {}
        # Old custom URLs may carry credentials. Never send them to the webview.
        try:
            endpoint = endpoint_url(str(entry["base_url"]), allow_http=name == "ollama") if entry.get("base_url") else ""
        except ValueError:
            endpoint = ""
        rows.append({
            "id": name, "label": PROVIDER_NAMES[name], "model": entry.get("model") or _default_model(name),
            "endpoint": endpoint, "configured": name == default or bool(entry),
            "has_key": bool(_resolve_api_key(entry, allow_generic_env=False, project_dir=project, provider=name)),
            "key_source": "Stored for this project" if entry.get("api_key_ref") else
                          ("Existing project credential" if entry.get("api_key") else
                           (entry.get("api_key_env") or _KEY_ENV.get(name, ""))),
        })
    cfg = config_from_mapping(project, raw)
    cc = cfg.execution.capsule
    kc = cfg.knowledge
    try:
        vector_url = endpoint_url(kc.vector_url) if kc.vector_url else ""
    except (TypeError, ValueError):
        vector_url = ""
    return {
        "provider": default, "providers": rows, "effective_provider": cfg.provider.type,
        "effective_model": cfg.provider.model,
        "provider_pin": bool(os.getenv("ARGUS_PROVIDER")),
        "environment_pin": bool(os.getenv("ARGUS_EXECUTION_ENVIRONMENT")),
        "time_minutes": cfg.time_minutes, "max_tokens": cfg.max_tokens,
        "environment": cfg.execution.environment,
        "capsule": {
            **{key: getattr(cc, key) for key in _CAPSULE_PATHS},
            "provider": cc.provider, "switch_name": cc.switch_name,
            "network_name": cc.switch_name or "argus-build",
            "libvirt_qemu_group": cc.libvirt_qemu_group, "libvirt_uri": cc.libvirt_uri,
            "image_format": cc.image_format, "retain_on_failure": cc.retain_on_failure,
        },
        "knowledge": {
            "enabled": kc.enabled, "type": kc.type, "vector_backend": kc.vector_backend,
            "persist_dir": kc.persist_dir or "", "embedding_model": kc.embedding_model, "vector_url": vector_url,
        },
    }


def update_model(raw: dict, values: dict, secret_ref: str) -> None:
    name = values.get("provider")
    if name not in PROVIDER_TYPES:
        raise ValueError("Select a model provider.")
    model = str(values.get("model", "")).strip()
    if not model or len(model) > 300:
        raise ValueError("Enter the model name or deployment ID.")
    endpoint = str(values.get("endpoint", "")).strip()
    if endpoint and name != "anthropic":
        endpoint = endpoint_url(endpoint, allow_http=name == "ollama")
    if name in {"litellm", "azure"} and not endpoint:
        raise ValueError("Enter the provider's base URL.")
    entries = mapping_section(raw, "providers")
    entry = mapping_section(entries, name)
    previous_endpoint = str(entry.get("base_url") or "").rstrip("/")
    if (name not in {"anthropic", "ollama"} and endpoint != previous_endpoint
            and os.getenv("ARGUS_API_KEY") and not os.getenv("ARGUS_BASE_URL")
            and (values.get("use_by_default") or name == (os.getenv("ARGUS_PROVIDER") or raw.get("provider") or "ollama"))):
        raise ValueError("Host settings supply the active key. A custom endpoint requires a matching host endpoint.")
    if (name != "anthropic" and endpoint != previous_endpoint and not secret_ref
            and not values.get("clear_key") and (entry.get("api_key_ref") or entry.get("api_key"))):
        raise ValueError("Enter the key again or clear the saved key when changing the endpoint.")
    entry["model"] = model
    if name != "anthropic":
        if endpoint:
            entry["base_url"] = endpoint
        else:
            entry.pop("base_url", None)
    if name in _KEY_ENV and (not endpoint or name in {"azure", "litellm"}):
        entry.setdefault("api_key_env", _KEY_ENV[name])
    elif endpoint and endpoint != previous_endpoint:
        entry.pop("api_key_env", None)
    if secret_ref or values.get("clear_key"):
        entry.pop("api_key", None)
        entry.pop("api_key_ref", None)
    if secret_ref:
        entry["api_key_ref"] = secret_ref
    if values.get("use_by_default"):
        raw["provider"] = name


def update_defaults(raw: dict, values: dict) -> None:
    minutes = values.get("time_minutes")
    minutes = None if minutes in {None, ""} else float(minutes)
    if minutes is not None and (not math.isfinite(minutes) or not 0 < minutes <= 240):
        raise ValueError("Choose a run duration greater than 0 and at most 240 minutes.")
    tokens = values.get("max_tokens")
    tokens = None if tokens in {None, ""} else int(str(tokens))
    if tokens is not None and tokens < 1:
        raise ValueError("The token limit must be at least 1.")
    mapping_section(raw, "budgets").update(time_minutes=minutes, max_tokens=tokens)
    environment = values.get("environment")
    if environment not in {"local", "capsule"}:
        raise ValueError("Choose Local or Capsule execution.")
    execution = mapping_section(raw, "execution")
    execution["environment"] = environment
    capsule = mapping_section(execution, "capsule")
    capsule["retain_on_failure"] = values.get("retain") is True
    knowledge = mapping_section(raw, "knowledge")
    knowledge["enabled"] = values.get("memory") is True
    kind = values.get("knowledge_type")
    if kind in {"json", "auto", "local", "docker", "external"}:
        knowledge["type"] = kind
        if kind == "external":
            url = str(values.get("vector_url") or "").strip()
            knowledge["vector_url"] = endpoint_url(url)
        model = str(values.get("embedding_model") or "").strip()
        if model and kind != "json":
            knowledge["embedding_model"] = model


def update_capsule(raw: dict, values: dict, project: Path, secret_ref: str) -> None:
    provider = values.get("provider")
    if provider not in {"auto", "libvirt", "hyperv"}:
        raise ValueError("Choose a Capsule provider.")
    capsule = mapping_section(mapping_section(raw, "execution"), "capsule")
    path = str(values.get("image") or "").strip()
    suffix = Path(path).suffix.lower()
    valid = {"auto": {".vhdx", ".qcow2", ".raw"}, "hyperv": {".vhdx"}, "libvirt": {".qcow2", ".raw"}}
    if suffix == ".iso":
        raise ValueError("Open Build from ISO to create an image from installation media.")
    # The legacy image factory resolves these two paths from the process cwd.
    image = (project / Path(path).expanduser()).resolve()
    if suffix not in valid[provider] or not image.is_file():
        raise ValueError("Select an existing virtual disk matching the Capsule provider.")
    ca = str(values.get("guest_ca_cert") or "").strip()
    ca_path = (project / Path(ca).expanduser()).resolve()
    if not ca or not ca_path.is_file():
        raise ValueError("Select the prepared guest's HTTPS CA certificate.")
    capsule.update(provider=provider, image=str(image), guest_ca_cert=str(ca_path),
                   guest_transport="https", allow_insecure_http=False, rotate_session_token=True)
    for key in ("switch_name", "vm_root", "libvirt_qemu_group"):
        capsule[key] = str(values.get(key) or "").strip()
    if capsule["vm_root"]:
        capsule["vm_root"] = str((project / Path(capsule["vm_root"]).expanduser()).resolve())
    # An explicit switch to a prepared disk removes only the ISO image binding.
    for key in ("environment_definition", "image_cache_root", "provisioning_evidence_root", "image_format"):
        capsule.pop(key, None)
    if secret_ref:
        capsule["guest_token_ref"] = secret_ref
