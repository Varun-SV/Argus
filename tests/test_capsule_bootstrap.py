from __future__ import annotations

from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError
from argus.capsule.bootstrap import (
    create_bootstrap_attempt,
    create_bootstrap_iso,
    load_bootstrap_manifest,
)
from argus.capsule.control import new_capsule_id
from argus.capsule.guest import CapsuleGuestError
from argus.capsule.secure_client import SecureGuestAgentClient


_RUNTIME_ID = "runtime-sha256-" + "a" * 64


def test_bootstrap_attempt_has_fresh_secrets_and_secret_free_manifest(
    tmp_path: Path,
) -> None:
    capsule_id = new_capsule_id()
    first = create_bootstrap_attempt(
        tmp_path,
        capsule_id=capsule_id,
        control_generation=1,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
        session_id="session-1",
    )
    second = create_bootstrap_attempt(
        tmp_path,
        capsule_id=capsule_id,
        control_generation=2,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
        session_id="session-2",
    )
    try:
        assert first.bootstrap_token != second.bootstrap_token
        assert first.tls_cert_sha256 != second.tls_cert_sha256
        raw = first.manifest_path.read_text(encoding="utf-8")
        assert first.bootstrap_token not in raw
        assert "PRIVATE KEY" not in raw
        parsed = load_bootstrap_manifest(first.root)
        assert parsed.capsule_id == capsule_id
        assert parsed.control_generation == 1
        assert parsed.tls_cert_sha256 == first.tls_cert_sha256
    finally:
        first.destroy()
        second.destroy()


def test_bootstrap_attempt_destroy_is_one_attempt_cleanup(tmp_path: Path) -> None:
    attempt = create_bootstrap_attempt(
        tmp_path,
        capsule_id=new_capsule_id(),
        control_generation=7,
        execution_mode="shared_user",
        runtime_identity=_RUNTIME_ID,
    )
    root = attempt.root
    assert root.is_dir()
    attempt.destroy()
    assert not root.exists()


def test_bootstrap_manifest_rejects_certificate_substitution(
    tmp_path: Path,
) -> None:
    first = create_bootstrap_attempt(
        tmp_path,
        capsule_id=new_capsule_id(),
        control_generation=1,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
    )
    second = create_bootstrap_attempt(
        tmp_path,
        capsule_id=new_capsule_id(),
        control_generation=1,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
    )
    try:
        first.tls_cert_path.write_bytes(second.tls_cert_path.read_bytes())
        with pytest.raises(CapsuleError, match="certificate digest mismatch"):
            load_bootstrap_manifest(first.root)
    finally:
        for path in (
            first.tls_cert_path,
            first.tls_key_path,
            first.token_path,
            first.manifest_path,
        ):
            path.unlink(missing_ok=True)
        if first.root.exists():
            first.root.rmdir()
        second.destroy()


def test_secure_client_requires_one_tls_trust_model(tmp_path: Path) -> None:
    cert = tmp_path / "legacy.pem"
    cert.write_text("not used by injected opener", encoding="utf-8")
    with pytest.raises(CapsuleGuestError, match="cannot combine"):
        SecureGuestAgentClient(
            "https://127.0.0.1:8765",
            "x" * 32,
            ca_cert_path=str(cert),
            pinned_cert_sha256="b" * 64,
            opener=lambda *args, **kwargs: None,
        )
    with pytest.raises(CapsuleGuestError, match="lowercase SHA-256"):
        SecureGuestAgentClient(
            "https://127.0.0.1:8765",
            "x" * 32,
            pinned_cert_sha256="not-a-digest",
            opener=lambda *args, **kwargs: None,
        )


def test_bootstrap_attempt_can_be_rendered_as_removable_iso(
    tmp_path: Path,
) -> None:
    attempt = create_bootstrap_attempt(
        tmp_path / "attempts",
        capsule_id=new_capsule_id(),
        control_generation=1,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
    )
    output = tmp_path / "bootstrap.iso"
    try:
        create_bootstrap_iso(attempt.root, output)
        data = output.read_bytes()
        assert data[16 * 2048 + 1:16 * 2048 + 6] == b"CD001"
        assert b"ARGUS_BOOTSTRAP" in data
        assert attempt.bootstrap_token.encode("utf-8") in data
        # Read the directory records as an ISO reader would, then apply Linux
        # ISO-9660 case folding/version removal. Never use source filenames.
        primary = data[16 * 2048:17 * 2048]
        extent = int.from_bytes(primary[158:162], "little")
        size = int.from_bytes(primary[166:170], "little")
        directory = data[extent * 2048:extent * 2048 + size]
        mounted = tmp_path / "mounted"
        mounted.mkdir()
        offset = 0
        while offset < len(directory) and directory[offset]:
            record = directory[offset:offset + directory[offset]]
            identifier = record[33:33 + record[32]]
            if identifier not in {b"\x00", b"\x01"}:
                name = identifier.decode("ascii").lower().removesuffix(";1")
                start = int.from_bytes(record[2:6], "little") * 2048
                length = int.from_bytes(record[10:14], "little")
                (mounted / name).write_bytes(data[start:start + length])
            offset += len(record)
        from argus.capsule.bootstrap import load_bootstrap_manifest
        from argus.capsule.bootstrap_service import _stage_from_root

        assert {path.name for path in mounted.iterdir()} == {
            "bootstrap.json", "bootstrap.token", "tls-cert.pem", "tls-key.pem"
        }
        assert load_bootstrap_manifest(mounted) == attempt.manifest
        staged = _stage_from_root(mounted, tmp_path / "staging")
        assert (staged / "bootstrap.token").read_bytes() == attempt.token_path.read_bytes()
        assert (staged / "tls-key.pem").read_bytes() == attempt.tls_key_path.read_bytes()
    finally:
        output.unlink(missing_ok=True)
        attempt.destroy()
