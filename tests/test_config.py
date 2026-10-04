from argus.config import init_project, load_config
from argus.tokens import TokenTracker


def test_init_creates_scaffold(tmp_path):
    argus_dir = init_project(tmp_path)
    assert (argus_dir / "config.yaml").exists()
    assert (argus_dir / "notepad.test.yaml").exists()
    assert (argus_dir / "runs").is_dir()
    assert (argus_dir / "roam").is_dir()
    # idempotent
    init_project(tmp_path)


def test_load_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("ARGUS_MODEL", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.provider.type == "ollama"
    assert cfg.provider.model == "gemma3:9b"


def test_libvirt_qemu_group_survives_config_and_environment_override(tmp_path, monkeypatch):
    import argus.execution

    init_project(tmp_path)
    (tmp_path / ".argus" / "config.yaml").write_text(
        "execution:\n  environment: capsule\n  capsule:\n"
        "    provider: libvirt\n    libvirt_qemu_group: argus-qemu\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ARGUS_CAPSULE_LIBVIRT_QEMU_GROUP", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.execution.capsule.libvirt_qemu_group == "argus-qemu"
    monkeypatch.setattr(
        argus.execution, "create_execution_environment",
        lambda *args, **kwargs: kwargs["capsule_config"],
    )
    assert cfg.make_execution_environment("cli")["libvirt_qemu_group"] == "argus-qemu"
    monkeypatch.setenv("ARGUS_CAPSULE_LIBVIRT_QEMU_GROUP", "service-qemu")
    assert cfg.make_execution_environment("cli")["libvirt_qemu_group"] == "service-qemu"


def test_load_from_scaffold(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    monkeypatch.delenv("ARGUS_MODEL", raising=False)
    init_project(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.provider.type == "ollama"
    assert cfg.time_minutes == 10
    assert cfg.max_tokens is None


def test_env_overrides(tmp_path, monkeypatch):
    init_project(tmp_path)
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    monkeypatch.setenv("ARGUS_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("ARGUS_API_KEY", "sk-test")
    cfg = load_config(tmp_path)
    assert cfg.provider.type == "openai"
    assert cfg.provider.model == "gpt-4o-mini"
    assert cfg.provider.api_key == "sk-test"


def test_ollama_budget_ignores_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("ARGUS_PROVIDER", raising=False)
    init_project(tmp_path)
    cfg = load_config(tmp_path)
    tracker = TokenTracker()
    budget = cfg.make_budget(tracker, time_minutes=5, max_tokens=1000)
    assert budget.max_seconds == 300
    assert budget.max_tokens is None  # ollama: token caps ignored


def test_paid_provider_budget_keeps_tokens(tmp_path, monkeypatch):
    init_project(tmp_path)
    monkeypatch.setenv("ARGUS_PROVIDER", "openai")
    monkeypatch.setenv("ARGUS_API_KEY", "sk-test")
    cfg = load_config(tmp_path)
    budget = cfg.make_budget(TokenTracker(), time_minutes=5, max_tokens=1000)
    assert budget.max_tokens == 1000
