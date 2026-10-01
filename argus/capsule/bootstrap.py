"""Per-generation Capsule bootstrap material and G1 TLS identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from argus.capsule.base import CapsuleError
from argus.capsule.control import CapsuleExecutionMode, validate_capsule_id
from argus.capsule.files import validate_session_id
from argus.capsule.iso9660 import write_root_iso


BOOTSTRAP_SCHEMA_VERSION = "argus-bootstrap-v1"
_RUNTIME_ID_RE = re.compile(r"^runtime-sha256-[0-9a-f]{64}$")
_CERT_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CapsuleBootstrapManifest:
    schema_version: str
    capsule_id: str
    control_generation: int
    session_id: str
    execution_mode: str
    created_at: str
    runtime_identity: str
    tls_cert_sha256: str
    token_file: str = "bootstrap.token"
    tls_cert_file: str = "tls-cert.pem"
    tls_key_file: str = "tls-key.pem"

    def __post_init__(self) -> None:
        if self.schema_version != BOOTSTRAP_SCHEMA_VERSION:
            raise CapsuleError("unsupported Capsule bootstrap schema")
        object.__setattr__(self, "capsule_id", validate_capsule_id(self.capsule_id))
        if (
            isinstance(self.control_generation, bool)
            or not isinstance(self.control_generation, int)
            or self.control_generation < 1
        ):
            raise CapsuleError("Capsule bootstrap generation must be positive")
        raw_session = str(self.session_id or "")
        session_id = validate_session_id(raw_session)
        if session_id != raw_session:
            raise CapsuleError("Capsule bootstrap session ID must be canonical")
        object.__setattr__(self, "session_id", session_id)
        try:
            mode = CapsuleExecutionMode(str(self.execution_mode).strip().lower())
        except ValueError as exc:
            raise CapsuleError("Capsule bootstrap execution mode is invalid") from exc
        object.__setattr__(self, "execution_mode", mode.value)
        if not _RUNTIME_ID_RE.fullmatch(str(self.runtime_identity or "")):
            raise CapsuleError("Capsule bootstrap runtime identity is invalid")
        if not _CERT_SHA256_RE.fullmatch(str(self.tls_cert_sha256 or "")):
            raise CapsuleError("Capsule bootstrap TLS certificate digest is invalid")
        for name in ("token_file", "tls_cert_file", "tls_key_file"):
            value = str(getattr(self, name) or "")
            if (
                not value
                or "/" in value
                or "\\" in value
                or value in {".", ".."}
            ):
                raise CapsuleError(f"Capsule bootstrap {name} must be a leaf filename")
        if not str(self.created_at or "").strip():
            raise CapsuleError("Capsule bootstrap created_at is missing")

    @classmethod
    def from_mapping(cls, raw: dict) -> "CapsuleBootstrapManifest":
        if not isinstance(raw, dict):
            raise CapsuleError("Capsule bootstrap manifest must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise CapsuleError(
                "unknown Capsule bootstrap manifest field(s): " + ", ".join(unknown)
            )
        try:
            return cls(**raw)
        except TypeError as exc:
            raise CapsuleError("Capsule bootstrap manifest shape is invalid") from exc

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CapsuleBootstrapAttempt:
    root: Path
    manifest: CapsuleBootstrapManifest
    manifest_path: Path
    token_path: Path
    tls_cert_path: Path
    tls_key_path: Path
    bootstrap_token: str

    @property
    def tls_cert_sha256(self) -> str:
        return self.manifest.tls_cert_sha256

    def destroy(self) -> None:
        for path in (
            self.token_path,
            self.tls_key_path,
            self.tls_cert_path,
            self.manifest_path,
        ):
            try:
                if path.exists():
                    path.chmod(0o600)
                    path.unlink()
            except OSError as exc:
                raise CapsuleError(
                    f"cannot destroy Capsule bootstrap material {path.name}: {exc}"
                ) from exc
        try:
            self.root.rmdir()
        except OSError as exc:
            raise CapsuleError(
                f"cannot remove Capsule bootstrap workspace {self.root}: {exc}"
            ) from exc


def _private_write(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(fd)
    if os.name == "posix":
        path.chmod(mode)


def _certificate_identity(
    capsule_id: str,
    generation: int,
) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Argus {capsule_id} generation {generation}",
            )
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem, sha256(cert_der).hexdigest()


def create_bootstrap_attempt(
    parent: str | Path,
    *,
    capsule_id: str,
    control_generation: int,
    execution_mode: str,
    runtime_identity: str,
    session_id: str | None = None,
) -> CapsuleBootstrapAttempt:
    """Create one non-reusable bootstrap attempt in a private host workspace."""
    capsule_id = validate_capsule_id(capsule_id)
    if (
        isinstance(control_generation, bool)
        or not isinstance(control_generation, int)
        or control_generation < 1
    ):
        raise CapsuleError("Capsule bootstrap generation must be positive")
    try:
        mode = CapsuleExecutionMode(str(execution_mode).strip().lower())
    except ValueError as exc:
        raise CapsuleError("Capsule bootstrap execution mode is invalid") from exc
    if not _RUNTIME_ID_RE.fullmatch(str(runtime_identity or "")):
        raise CapsuleError("Capsule bootstrap runtime identity is invalid")
    resolved_session = session_id or uuid4().hex
    raw_session = str(resolved_session)
    if validate_session_id(raw_session) != raw_session:
        raise CapsuleError("Capsule bootstrap session ID must be canonical")

    parent_path = Path(parent).expanduser().resolve()
    if parent_path.exists() and (parent_path.is_symlink() or not parent_path.is_dir()):
        raise CapsuleError("Capsule bootstrap parent must be a directory")
    parent_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        parent_path.chmod(0o700)

    root = parent_path / f"{capsule_id}-g{control_generation}-{uuid4().hex}"
    root.mkdir(mode=0o700)
    token_path = root / "bootstrap.token"
    cert_path = root / "tls-cert.pem"
    key_path = root / "tls-key.pem"
    manifest_path = root / "bootstrap.json"
    try:
        token = secrets.token_urlsafe(48)
        cert_pem, key_pem, cert_sha256 = _certificate_identity(
            capsule_id, control_generation
        )
        manifest = CapsuleBootstrapManifest(
            schema_version=BOOTSTRAP_SCHEMA_VERSION,
            capsule_id=capsule_id,
            control_generation=control_generation,
            session_id=raw_session,
            execution_mode=mode.value,
            created_at=datetime.now(timezone.utc).isoformat(),
            runtime_identity=runtime_identity,
            tls_cert_sha256=cert_sha256,
        )
        _private_write(token_path, (token + "\n").encode("utf-8"), 0o600)
        _private_write(key_path, key_pem, 0o600)
        _private_write(cert_path, cert_pem, 0o644)
        _private_write(
            manifest_path,
            (
                json.dumps(
                    manifest.to_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
            0o600,
        )
        return CapsuleBootstrapAttempt(
            root=root.resolve(),
            manifest=manifest,
            manifest_path=manifest_path.resolve(),
            token_path=token_path.resolve(),
            tls_cert_path=cert_path.resolve(),
            tls_key_path=key_path.resolve(),
            bootstrap_token=token,
        )
    except BaseException:
        for path in (token_path, key_path, cert_path, manifest_path):
            path.unlink(missing_ok=True)
        root.rmdir()
        raise


def load_bootstrap_manifest(root: str | Path) -> CapsuleBootstrapManifest:
    path = Path(root) / "bootstrap.json"
    if path.is_symlink():
        raise CapsuleError("Capsule bootstrap manifest cannot be a symlink")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("Capsule bootstrap manifest is unreadable or invalid") from exc
    manifest = CapsuleBootstrapManifest.from_mapping(raw)
    cert_path = Path(root) / manifest.tls_cert_file
    if cert_path.is_symlink():
        raise CapsuleError("Capsule bootstrap certificate cannot be a symlink")
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CapsuleError("Capsule bootstrap certificate is invalid") from exc
    actual = sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    if actual != manifest.tls_cert_sha256:
        raise CapsuleError("Capsule bootstrap certificate digest mismatch")
    return manifest


def create_bootstrap_iso(
    bootstrap_root: str | Path,
    output: str | Path,
) -> Path:
    """Convert one verified attempt directory into removable control media."""
    root = Path(bootstrap_root)
    manifest = load_bootstrap_manifest(root)
    return write_root_iso(
        output,
        volume_label="ARGUS_BOOTSTRAP",
        files=(
            ("BOOTSTRAP.JSON;1", root / "bootstrap.json"),
            ("TOKEN.TXT;1", root / manifest.token_file),
            ("CERT.PEM;1", root / manifest.tls_cert_file),
            ("KEY.PEM;1", root / manifest.tls_key_file),
        ),
    )
