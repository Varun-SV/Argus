"""Per-user settings: providers, API keys, the active model and a few options.

Everything lives in one JSON file in the user's configuration directory, so
nothing has to be edited by hand and no project file ever contains a key.
API keys are written to a file only the current user can read; an environment
variable (for example ``ANTHROPIC_API_KEY``) is used when no key is saved.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# kind: which wire protocol the provider speaks.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "env": "ANTHROPIC_API_KEY",
        "needs_key": True,
        "suggested_model": "claude-opus-5-5",
        "key_help": "Create a key at console.anthropic.com → API keys.",
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "needs_key": True,
        "suggested_model": "",
        "key_help": "Create a key at platform.openai.com → API keys.",
    },
    "gemini": {
        "label": "Google Gemini",
        "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY",
        "needs_key": True,
        "suggested_model": "",
        "key_help": "Create a key at aistudio.google.com → Get API key.",
    },
    "ollama": {
        "label": "Ollama (local)",
        "kind": "openai",
        "base_url": "http://localhost:11434/v1",
        "env": "",
        "needs_key": False,
        "suggested_model": "",
        "key_help": "No key needed. Ollama must be running on this computer.",
    },
    "custom": {
        "label": "OpenAI-compatible server",
        "kind": "openai",
        "base_url": "",
        "env": "ARGUS_LLM_API_KEY",
        "needs_key": False,
        "suggested_model": "",
        "key_help": "LiteLLM, LM Studio, vLLM, Azure OpenAI v1 … enter its base URL (ending in /v1).",
    },
}

DEFAULTS: Dict[str, Any] = {
    "provider": "",
    "model": "",
    "vision": True,
    "headless": True,
    "max_actions": 15,
    "workspace": "",
    "recent_workspaces": [],
    "providers": {},
}


def config_dir() -> Path:
    override = os.environ.get("ARGUS2_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Argus"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Argus"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "argus"


def mask(key: str) -> str:
    if not key:
        return ""
    return "•" * 8 + key[-4:] if len(key) > 8 else "•" * 8


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else config_dir() / "settings.json"
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load_error = ""
        self._load()

    # -- storage ---------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # Never crash on a damaged file: keep it aside and start clean.
            self.load_error = f"Settings file could not be read ({exc}); starting with defaults."
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass
            return
        if isinstance(raw, dict):
            for key, default in DEFAULTS.items():
                value = raw.get(key, default)
                if isinstance(value, type(default)):
                    self.data[key] = value

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.data, handle, indent=2)
                if os.name != "nt":
                    os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # -- providers -------------------------------------------------------
    def _saved(self, provider: str) -> Dict[str, Any]:
        value = self.data["providers"].get(provider)
        return value if isinstance(value, dict) else {}

    def resolve(self, provider: str) -> Dict[str, Any]:
        """Effective connection details for a provider (includes the key)."""
        if provider not in PROVIDERS:
            raise SettingsError(f"Unknown provider '{provider}'.")
        spec = PROVIDERS[provider]
        saved = self._saved(provider)
        key = str(saved.get("api_key") or "")
        source = "saved" if key else ""
        if not key and spec["env"] and os.environ.get(spec["env"]):
            key, source = os.environ[spec["env"]], "environment"
        base_url = str(saved.get("base_url") or spec["base_url"]).rstrip("/")
        return {
            "provider": provider, "kind": spec["kind"], "label": spec["label"],
            "base_url": base_url, "api_key": key, "key_source": source,
            "needs_key": spec["needs_key"],
        }

    def provider_ready(self, provider: str) -> str:
        """Empty string when usable, otherwise what is missing."""
        info = self.resolve(provider)
        if info["needs_key"] and not info["api_key"]:
            return f"{info['label']} has no API key yet."
        if info["kind"] == "openai" and not info["base_url"]:
            return f"{info['label']} has no base URL yet."
        return ""

    def set_key(self, provider: str, api_key: str) -> None:
        if provider not in PROVIDERS:
            raise SettingsError(f"Unknown provider '{provider}'.")
        api_key = (api_key or "").strip()
        if any(ch.isspace() for ch in api_key):
            raise SettingsError("An API key cannot contain spaces or line breaks.")
        with self._lock:
            saved = dict(self._saved(provider))
            if api_key:
                saved["api_key"] = api_key
            else:
                saved.pop("api_key", None)
            self.data["providers"][provider] = saved
            self.save()

    def set_base_url(self, provider: str, base_url: str) -> None:
        if provider not in PROVIDERS:
            raise SettingsError(f"Unknown provider '{provider}'.")
        base_url = (base_url or "").strip().rstrip("/")
        if base_url and not base_url.lower().startswith(("http://", "https://")):
            raise SettingsError("The base URL must start with http:// or https://")
        with self._lock:
            saved = dict(self._saved(provider))
            if base_url and base_url != PROVIDERS[provider]["base_url"]:
                saved["base_url"] = base_url
            else:
                saved.pop("base_url", None)
            self.data["providers"][provider] = saved
            self.save()

    def remember_models(self, provider: str, models: List[str]) -> None:
        with self._lock:
            saved = dict(self._saved(provider))
            saved["models"] = list(models)[:500]
            self.data["providers"][provider] = saved
            self.save()

    # -- general options -------------------------------------------------
    def update(self, changes: Dict[str, Any]) -> None:
        with self._lock:
            if "provider" in changes:
                provider = str(changes["provider"] or "")
                if provider and provider not in PROVIDERS:
                    raise SettingsError(f"Unknown provider '{provider}'.")
                self.data["provider"] = provider
            if "model" in changes:
                self.data["model"] = str(changes["model"] or "").strip()
            for flag in ("vision", "headless"):
                if flag in changes:
                    self.data[flag] = bool(changes[flag])
            if "max_actions" in changes:
                try:
                    number = int(changes["max_actions"])
                except (TypeError, ValueError):
                    raise SettingsError("Actions per step must be a number.") from None
                if not 1 <= number <= 100:
                    raise SettingsError("Actions per step must be between 1 and 100.")
                self.data["max_actions"] = number
            self.save()

    def set_workspace(self, path: Path) -> None:
        with self._lock:
            text = str(path)
            recent = [text] + [p for p in self.data["recent_workspaces"] if p != text]
            self.data["workspace"] = text
            self.data["recent_workspaces"] = recent[:8]
            self.save()

    def model_ready(self) -> str:
        """Empty string when a model can be called, otherwise the reason."""
        provider, model = self.data["provider"], self.data["model"]
        if not provider:
            return "No model is set up yet. Open Settings and choose a provider and model."
        missing = self.provider_ready(provider)
        if missing:
            return missing + " Add it in Settings."
        if not model:
            return "No model is selected. Open Settings and pick one."
        return ""

    def public(self) -> Dict[str, Any]:
        """Settings for the UI. Never contains a key."""
        providers = []
        for name, spec in PROVIDERS.items():
            info = self.resolve(name)
            saved = self._saved(name)
            providers.append({
                "id": name, "label": spec["label"], "needs_key": spec["needs_key"],
                "key_help": spec["key_help"], "env": spec["env"],
                "key_set": bool(info["api_key"]), "key_source": info["key_source"],
                "key_hint": mask(info["api_key"]),
                "base_url": info["base_url"], "default_base_url": spec["base_url"],
                "base_url_editable": name in ("ollama", "custom", "openai", "anthropic"),
                "suggested_model": spec["suggested_model"],
                "models": saved.get("models") if isinstance(saved.get("models"), list) else [],
                "missing": self.provider_ready(name),
            })
        return {
            "provider": self.data["provider"], "model": self.data["model"],
            "vision": self.data["vision"], "headless": self.data["headless"],
            "max_actions": self.data["max_actions"],
            "providers": providers, "model_missing": self.model_ready(),
            "file": str(self.path), "load_error": self.load_error,
            "recent_workspaces": list(self.data["recent_workspaces"]),
        }
