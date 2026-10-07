from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from argus.cli import main
from argus.config import load_config
from argus.secrets import ArgusSecretStore, SecretStoreError


def test_secret_store_roundtrip_replace_and_remove(tmp_path: Path) -> None:
    store = ArgusSecretStore(tmp_path / "store")
    ref = "secret://argus/guest/bootstrap"
    store.set(ref, "first-token-value")
    assert store.get(ref) == "first-token-value"
    store.set(ref, "replacement-token-value")
    assert store.get(ref) == "replacement-token-value"
    assert store.list_refs() == (ref,)
    assert b"replacement-token-value" not in (store.root / "secrets.sqlite3").read_bytes()
    assert store.delete(ref)
    assert not store.delete(ref)
    with pytest.raises(SecretStoreError, match="not found"):
        store.get(ref)


def test_secret_store_rejects_unscoped_refs_and_symlink(tmp_path: Path) -> None:
    store = ArgusSecretStore(tmp_path / "store")
    with pytest.raises(SecretStoreError, match="secret://argus"):
        store.set("secret://ates/unrelated", "value")
    with pytest.raises(SecretStoreError, match="secret://argus"):
        store.set("secret://argus/../escape", "value")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(SecretStoreError, match="symlink"):
        ArgusSecretStore(link).set("secret://argus/test", "value")


def test_secret_store_rejects_modified_ciphertext(tmp_path: Path) -> None:
    store = ArgusSecretStore(tmp_path / "store")
    ref = "secret://argus/test"
    store.set(ref, "sensitive-value")
    with sqlite3.connect(store.root / "secrets.sqlite3") as connection:
        protected = connection.execute(
            "SELECT protected FROM secrets WHERE ref=?", (ref,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE secrets SET protected=? WHERE ref=?",
            (protected[:-1] + bytes((protected[-1] ^ 1,)), ref),
        )
    with pytest.raises(SecretStoreError):
        store.get(ref)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode checks")
def test_secret_store_uses_private_posix_modes(tmp_path: Path) -> None:
    store = ArgusSecretStore(tmp_path / "store")
    store.set("secret://argus/test", "value")
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / "secrets.key").stat().st_mode & 0o777 == 0o600
    assert (store.root / "secrets.sqlite3").stat().st_mode & 0o777 == 0o600
    (store.root / "secrets.key").chmod(0o644)
    with pytest.raises(SecretStoreError, match="unsafe permissions"):
        store.get("secret://argus/test")


def test_secrets_cli_updates_without_echoing_value(tmp_path: Path, monkeypatch) -> None:
    import argus.cli as cli

    monkeypatch.setattr(cli, "ArgusSecretStore", lambda: ArgusSecretStore(tmp_path / "store"))
    runner = CliRunner()
    ref = "secret://argus/test"
    first = runner.invoke(main, ["secrets", "set", ref], input="private-one\nprivate-one\n")
    assert first.exit_code == 0, first.output
    assert "private-one" not in first.output
    second = runner.invoke(main, ["secrets", "set", ref, "--stdin"], input="private-two")
    assert second.exit_code == 0, second.output
    assert "private-two" not in second.output
    assert ArgusSecretStore(tmp_path / "store").get(ref) == "private-two"
    listed = runner.invoke(main, ["secrets", "list"])
    assert listed.exit_code == 0
    assert ref in listed.output
    assert "private-two" not in listed.output


def test_capsule_config_resolves_guest_token_from_user_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "stale-environment-token")
    ref = "secret://argus/capsule/bootstrap"
    ArgusSecretStore().set(ref, "stored-bootstrap-token")
    config_dir = tmp_path / "project" / ".argus"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "provider: ollama\nexecution:\n  environment: capsule\n"
        f"  capsule:\n    guest_token_ref: {ref}\n",
        encoding="utf-8",
    )

    def capture_environment(adapter_type, *, environment_type, capsule_config):
        assert adapter_type == "cli"
        assert environment_type == "capsule"
        return capsule_config

    monkeypatch.setattr("argus.execution.create_execution_environment", capture_environment)
    config = load_config(tmp_path / "project")
    settings = config.make_execution_environment("cli")
    assert settings["guest_token"] == "stored-bootstrap-token"
