"""TLS-enabled Argus Capsule guest-agent entrypoint.

The PR5 guest protocol is reused unchanged. PR6 adds two control-plane
properties around it:

* non-loopback service defaults to TLS and refuses plaintext unless explicitly
  opted into for disposable development; and
* the reusable bootstrap bearer can be rotated exactly once to a random
  session-specific bearer over the authenticated TLS channel.
"""

from __future__ import annotations

import argparse
import ssl
from http import HTTPStatus
from http.server import ThreadingHTTPServer

from argus.adapters.base import AdapterError
from argus.capsule.files import validate_session_id
from argus.capsule.guest_agent import (
    GuestAgentHandler,
    GuestAgentServer,
    GuestAgentState,
    _consume_control_token,
)


class SecureGuestAgentServer(GuestAgentServer):
    def __init__(self, address, token: str, state=None):
        self.token = token
        self.state = state or GuestAgentState()
        self.auth_session_id = ""
        ThreadingHTTPServer.__init__(self, address, SecureGuestAgentHandler)


class SecureGuestAgentHandler(GuestAgentHandler):
    server: SecureGuestAgentServer

    def _rotate_auth(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        body = self._payload()
        session_id = validate_session_id(str(body.get("session_id") or ""))
        token = str(body.get("token") or "").strip()
        if len(token) < 32:
            raise AdapterError("rotated session token must be at least 32 characters")
        if self.server.auth_session_id and self.server.auth_session_id != session_id:
            raise AdapterError("guest auth is already bound to another Capsule session")
        if self.server.auth_session_id == session_id:
            raise AdapterError("guest auth has already been rotated for this Capsule session")

        # Replace the only server-held bearer after authenticating this request.
        # The old reusable bootstrap value is no longer accepted after response.
        self.server.token = token
        self.server.auth_session_id = session_id
        self._send(HTTPStatus.OK, {"ok": True, "session_id": session_id})

    def _dispatch(self) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        if self.command == "POST" and parsed.path == "/v1/auth/rotate":
            try:
                self._rotate_auth()
            except (AdapterError, ValueError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        super()._dispatch()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Secure Argus Capsule guest agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", default="")
    parser.add_argument("--token-env", default="ARGUS_CAPSULE_GUEST_TOKEN")
    parser.add_argument("--tls-cert", default="")
    parser.add_argument("--tls-key", default="")
    parser.add_argument("--allow-insecure-http", action="store_true")
    args = parser.parse_args(argv)

    try:
        token = _consume_control_token(
            token_file=args.token_file,
            token_env=args.token_env,
        )
    except AdapterError as exc:
        parser.error(str(exc))

    if not (1 <= args.port <= 65535):
        parser.error("port must be between 1 and 65535")

    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    remote_binding = args.host not in loopback_hosts
    if remote_binding and not token:
        parser.error("a guest token is required when binding outside loopback")

    has_cert = bool(args.tls_cert)
    has_key = bool(args.tls_key)
    if has_cert != has_key:
        parser.error("--tls-cert and --tls-key must be provided together")
    if remote_binding and not has_cert and not args.allow_insecure_http:
        parser.error(
            "non-loopback Capsule control requires TLS; pass --tls-cert/--tls-key "
            "or explicitly opt into --allow-insecure-http for legacy development"
        )

    server = SecureGuestAgentServer((args.host, args.port), token)
    if has_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(args.tls_cert, args.tls_key)
        except (OSError, ssl.SSLError) as exc:
            server.server_close()
            parser.error(f"cannot load Capsule TLS certificate/key: {exc}")
        server.socket = context.wrap_socket(server.socket, server_side=True)

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.state.close()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
