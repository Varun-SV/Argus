"""Long-lived Node identity primitives for Argus Fleet.

Fleet v0.1 uses Ed25519 keys as the Node proof-of-possession identity.  The
private key remains on the Node; the Control Center persists only the public
key and its fingerprint.  Transport may use mTLS or a server-authenticated TLS
channel with these signed application requests as the client-authentication
layer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

FLEET_IDENTITY_VERSION = "argus-fleet-node-ed25519-v1"
PRIVATE_KEY_FILE_VERSION = "argus-fleet-node-key-v1"


class FleetIdentityError(ValueError):
    """A Node identity or proof-of-possession value is malformed or invalid."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise FleetIdentityError("encoded identity value must be a non-empty string")
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise FleetIdentityError("encoded identity value is not canonical base64url") from exc
    if _b64encode(raw) != value:
        raise FleetIdentityError("encoded identity value is not canonical base64url")
    return raw


def public_key_fingerprint(public_key_b64: str) -> str:
    raw = _b64decode(public_key_b64)
    if len(raw) != 32:
        raise FleetIdentityError("Ed25519 public key must contain exactly 32 bytes")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_signature(public_key_b64: str, message: bytes, signature_b64: str) -> None:
    if not isinstance(message, bytes):
        raise FleetIdentityError("signed Fleet message must be bytes")
    public_raw = _b64decode(public_key_b64)
    signature = _b64decode(signature_b64)
    if len(public_raw) != 32:
        raise FleetIdentityError("Ed25519 public key must contain exactly 32 bytes")
    if len(signature) != 64:
        raise FleetIdentityError("Ed25519 signature must contain exactly 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, message)
    except InvalidSignature as exc:
        raise FleetIdentityError("Node proof-of-possession signature is invalid") from exc
    except ValueError as exc:
        raise FleetIdentityError("Node public key is invalid") from exc


def canonical_signed_message(kind: str, payload: dict[str, object]) -> bytes:
    if not isinstance(kind, str) or not kind.strip():
        raise FleetIdentityError("signed message kind must be non-empty")
    envelope = {
        "fleet_identity_version": FLEET_IDENTITY_VERSION,
        "kind": kind,
        "payload": payload,
    }
    try:
        return json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise FleetIdentityError("signed Fleet payload is not canonical JSON") from exc


class NodeKeyPair:
    """An Ed25519 Node identity whose private material never leaves this object."""

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> "NodeKeyPair":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "NodeKeyPair":
        if not isinstance(raw, bytes) or len(raw) != 32:
            raise FleetIdentityError("Ed25519 private key must contain exactly 32 bytes")
        try:
            return cls(Ed25519PrivateKey.from_private_bytes(raw))
        except ValueError as exc:
            raise FleetIdentityError("Node private key is invalid") from exc

    @property
    def public_key_b64(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return _b64encode(raw)

    @property
    def fingerprint(self) -> str:
        return public_key_fingerprint(self.public_key_b64)

    def sign(self, message: bytes) -> str:
        if not isinstance(message, bytes):
            raise FleetIdentityError("signed Fleet message must be bytes")
        return _b64encode(self._private_key.sign(message))

    def private_bytes(self) -> bytes:
        return self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def save(self, path: Path | str) -> Path:
        """Atomically save the Node key without ever routing it through argv/env."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(destination.parent, 0o700)
            except OSError:
                pass

        payload = json.dumps(
            {
                "version": PRIVATE_KEY_FILE_VERSION,
                "private_key": _b64encode(self.private_bytes()),
                "public_key_fingerprint": self.fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"

        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        tmp_path = Path(tmp_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, destination)
            if os.name != "nt":
                os.chmod(destination, 0o600)
            return destination
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path | str) -> "NodeKeyPair":
        source = Path(path)
        if os.name != "nt":
            mode = source.stat().st_mode & 0o777
            if mode & 0o077:
                raise FleetIdentityError(
                    "Node private key file must not be accessible by group/other users"
                )
        try:
            record = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FleetIdentityError("Node private key file cannot be read safely") from exc
        if not isinstance(record, dict) or record.get("version") != PRIVATE_KEY_FILE_VERSION:
            raise FleetIdentityError("unsupported Node private key file")
        raw = _b64decode(record.get("private_key"))
        pair = cls.from_private_bytes(raw)
        if record.get("public_key_fingerprint") != pair.fingerprint:
            raise FleetIdentityError("Node private key file fingerprint does not match")
        return pair
