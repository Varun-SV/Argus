"""Secure host-side control client for an Argus Capsule guest.

PR6 keeps the existing JSON guest protocol but requires it to travel over a
pinned HTTPS channel by default. A reusable bootstrap bearer is used only long
enough to authenticate the freshly booted golden image; the client then rotates
to a random per-session bearer over that encrypted channel.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from argus.capsule.files import validate_session_id
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient


_PIN_RE = re.compile(r"^[0-9a-f]{64}$")


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, expected_sha256: str, **kwargs):
        self._expected_sha256 = expected_sha256
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise ssl.SSLError("Capsule TLS socket was not established")
        peer = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(peer).hexdigest()
        if not hmac.compare_digest(actual, self._expected_sha256):
            self.close()
            raise ssl.SSLError("Capsule TLS certificate pin mismatch")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, expected_sha256: str):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context)
        self._expected_sha256 = expected_sha256
        self._context = context

    def https_open(self, req):
        expected = self._expected_sha256
        context = self._context

        def factory(host, timeout=300, **kwargs):
            kwargs.pop("check_hostname", None)
            kwargs["context"] = context
            return _PinnedHTTPSConnection(
                host,
                timeout=timeout,
                expected_sha256=expected,
                **kwargs,
            )

        return self.do_open(factory, req)


class SecureGuestAgentClient(GuestAgentClient):
    """Guest client with pinned TLS trust and bearer rotation."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 15.0,
        ca_cert_path: str = "",
        pinned_cert_sha256: str = "",
        allow_insecure_http: bool = False,
        opener: Optional[Callable] = None,
        require_target_desktop: bool = False,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        scheme = parsed.scheme.lower()
        if scheme == "https":
            pin = str(pinned_cert_sha256 or "").strip().lower()
            ca_path = Path(ca_cert_path).expanduser() if ca_cert_path else None
            if pin and ca_path is not None:
                raise CapsuleGuestError(
                    "Capsule TLS trust cannot combine exact pinning with guest_ca_cert"
                )
            if pin:
                if not _PIN_RE.fullmatch(pin):
                    raise CapsuleGuestError(
                        "Capsule TLS certificate pin must be a lowercase SHA-256 digest"
                    )
                if opener is None:
                    opener = urllib.request.build_opener(
                        _PinnedHTTPSHandler(pin)
                    ).open
            else:
                if ca_path is None or not ca_path.is_file():
                    raise CapsuleGuestError(
                        "HTTPS Capsule control requires exact certificate pinning "
                        "or guest_ca_cert for the legacy CA-backed mode"
                    )
                if opener is None:
                    context = ssl.create_default_context(
                        cafile=str(ca_path.resolve())
                    )
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_REQUIRED
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPSHandler(context=context)
                    ).open
        elif scheme == "http":
            if not allow_insecure_http:
                raise CapsuleGuestError(
                    "plain HTTP Capsule control is disabled; use HTTPS or explicitly "
                    "set allow_insecure_http for legacy/disposable development only"
                )
        else:
            raise CapsuleGuestError(f"unsupported Capsule guest transport: {scheme!r}")

        super().__init__(
            endpoint,
            token,
            timeout_seconds=timeout_seconds,
            require_target_desktop=require_target_desktop,
            opener=opener,
        )
        self.transport_secure = scheme == "https"
        self.pinned_cert_sha256 = str(pinned_cert_sha256 or "").strip().lower()

    def rotate_session_token(
        self,
        session_id: str,
        new_token: str,
        *,
        capsule_id: str = "",
        control_generation: int = 0,
        execution_mode: str = "",
    ) -> None:
        """Replace the bootstrap bearer with a session-only bearer.

        Legacy sessions may probe with the proposed token after a lost reply.
        Generation-bound sessions must abandon the attempt instead: the host
        owns recovery by reserving a strictly higher generation.
        """
        session_id = validate_session_id(session_id)
        token = str(new_token or "").strip()
        if len(token) < 32:
            raise CapsuleGuestError("rotated Capsule session token is too short")

        previous = self.token
        try:
            payload = {"session_id": session_id, "token": token}
            if capsule_id:
                payload.update(
                    {
                        "capsule_id": capsule_id,
                        "control_generation": control_generation,
                        "execution_mode": execution_mode,
                    }
                )
            self._request("POST", "/v1/auth/rotate", payload)
        except Exception as rotate_exc:
            if capsule_id:
                # Neither the old bootstrap bearer nor the proposed active
                # bearer is usable by this abandoned production client.
                self.token = ""
                raise CapsuleGuestError(
                    "Capsule auth acknowledgement is uncertain; a new generation is required"
                ) from None
            self.token = token
            try:
                health = self.health()
                if health.get("auth_session_id") == session_id:
                    if capsule_id and (
                        health.get("capsule_id") != capsule_id
                        or int(health.get("control_generation") or 0)
                        != int(control_generation)
                    ):
                        raise CapsuleGuestError(
                            "guest committed a different Capsule control generation"
                        )
                    return
            except Exception:
                pass
            self.token = previous
            raise rotate_exc
        else:
            self.token = token

    def installed_packages(self, names: tuple[str, ...]) -> dict[str, str]:
        """Query installed Debian packages over the bound secure guest session."""
        result = self._request(
            "POST", "/v1/provisioning/packages", {"packages": list(names)},
        )
        installed = result.get("installed")
        if not isinstance(installed, dict) or any(
            not isinstance(name, str) or not isinstance(version, str)
            for name, version in installed.items()
        ):
            raise CapsuleGuestError("guest package inventory is invalid")
        return installed
