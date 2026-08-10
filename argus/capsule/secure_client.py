"""Secure host-side control client for an Argus Capsule guest.

PR6 keeps the existing JSON guest protocol but requires it to travel over a
pinned HTTPS channel by default. A reusable bootstrap bearer is used only long
enough to authenticate the freshly booted golden image; the client then rotates
to a random per-session bearer over that encrypted channel.
"""

from __future__ import annotations

import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from argus.capsule.files import validate_session_id
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient


class SecureGuestAgentClient(GuestAgentClient):
    """Guest client with pinned TLS trust and bearer rotation."""

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        timeout_seconds: float = 15.0,
        ca_cert_path: str = "",
        allow_insecure_http: bool = False,
        opener: Optional[Callable] = None,
    ) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        scheme = parsed.scheme.lower()
        if scheme == "https":
            ca_path = Path(ca_cert_path).expanduser() if ca_cert_path else None
            if ca_path is None or not ca_path.is_file():
                raise CapsuleGuestError(
                    "HTTPS Capsule control requires guest_ca_cert pointing to the "
                    "dedicated Argus guest CA/self-signed certificate"
                )
            if opener is None:
                context = ssl.create_default_context(cafile=str(ca_path.resolve()))
                # Capsule endpoints are provider-attested IPs. Trust is pinned to
                # the dedicated CA/certificate rather than DNS hostnames.
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
            opener=opener,
        )
        self.transport_secure = scheme == "https"

    def rotate_session_token(self, session_id: str, new_token: str) -> None:
        """Atomically replace the bootstrap bearer with a session-only bearer."""
        session_id = validate_session_id(session_id)
        token = str(new_token or "").strip()
        if len(token) < 32:
            raise CapsuleGuestError("rotated Capsule session token is too short")
        self._request(
            "POST",
            "/v1/auth/rotate",
            {"session_id": session_id, "token": token},
        )
        # Only switch locally after the guest confirmed the rotation under the
        # previous bearer. A failed response leaves bootstrap auth usable for
        # cleanup/retry instead of desynchronizing both ends.
        self.token = token
