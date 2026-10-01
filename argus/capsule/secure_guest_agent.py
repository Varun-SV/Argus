"""TLS-enabled Argus Capsule guest-agent entrypoint.

The PR5 guest protocol is reused unchanged. PR6 adds two control-plane
properties around it:

* non-loopback service defaults to TLS and refuses plaintext unless explicitly
  opted into for disposable development; and
* the reusable bootstrap bearer can be rotated exactly once to a random
  session-specific bearer over the authenticated TLS channel.

Runtime bootstrap material is consumed from the per-session disk and is never
written back while the application-under-test may have controlled the guest.
Secure Failure Capsules are therefore forensic disk/configuration evidence;
restarting them through the old control identity is intentionally unsupported.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import ssl
import subprocess
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from argus.adapters.base import AdapterError
from argus.capsule.bootstrap_service import prepare_bootstrap_service
from argus.capsule.control import GuestControlStateStore, validate_capsule_id
from argus.capsule.files import validate_session_id
from argus.capsule.guest_agent import (
    GuestAgentHandler,
    GuestAgentServer,
    GuestAgentState,
    _consume_control_token,
)


_DEB_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,127}$")


def _release_identity() -> dict[str, str | int]:
    """Report installed OS facts; unknown facts are omitted so callers fail closed."""
    if platform.system().lower() == "windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as key:
                build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
                release = str(winreg.QueryValueEx(key, "DisplayVersion")[0])
                edition = str(winreg.QueryValueEx(key, "EditionID")[0])
            if build >= 22000 and re.fullmatch(r"\d{2}H[12]", release):
                return {
                    "os_id": "windows-11", "os_release": release,
                    "os_edition": edition.lower(), "os_build": build,
                }
        except (OSError, ValueError, TypeError):
            return {}
        return {}
    if platform.system().lower() == "linux":
        try:
            info = platform.freedesktop_os_release()
            os_id, release = info.get("ID"), info.get("VERSION_ID")
            if os_id and release and re.fullmatch(r"[a-z0-9._-]{1,64}", os_id) and re.fullmatch(
                r"\d{2}\.\d{2}", release
            ):
                return {"os_id": os_id, "os_release": release}
        except (OSError, ValueError):
            return {}
    return {}



def _machine_identity() -> str:
    system = platform.system().lower()
    if system == "windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value = str(
                    winreg.QueryValueEx(key, "MachineGuid")[0]
                ).strip().lower()
            if re.fullmatch(r"[0-9a-f-]{32,64}", value):
                return value
        except (OSError, TypeError, ValueError):
            return ""
        return ""
    if system == "linux":
        try:
            value = Path("/etc/machine-id").read_text(
                encoding="ascii"
            ).strip().lower()
        except (OSError, UnicodeError):
            return ""
        if re.fullmatch(r"[0-9a-f]{32}", value) and value != "0" * 32:
            return value
    return ""


def _target_user_policy() -> dict[str, object]:
    system = platform.system().lower()
    if system == "windows":
        script = (
            "$ErrorActionPreference='Stop';$n='argus-target';"
            "$u=Get-LocalUser -Name $n -ErrorAction SilentlyContinue;"
            "if(-not $u){'missing';exit 0};"
            "$a=Get-LocalGroupMember -Group 'Administrators' "
            "-ErrorAction Stop|Where-Object Name -Match ('\\\\'+$n+'$');"
            "if($a){'admin'}else{'nonadmin'}"
        )
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        status = result.stdout.strip().splitlines()[-1:] or [""]
        return {
            "target_user": "argus-target",
            "target_user_present": status[0] in {"nonadmin", "admin"},
            "target_user_non_admin": status[0] == "nonadmin",
            "target_user_locked": False,
        }

    if system == "linux":
        try:
            import pwd

            account = pwd.getpwnam("argus")
            groups = subprocess.run(
                ["id", "-nG", "argus"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.split()
            shadow = Path("/etc/shadow").read_text(
                encoding="utf-8", errors="strict"
            )
            entry = next(
                line for line in shadow.splitlines()
                if line.startswith("argus:")
            )
            password_field = entry.split(":", 2)[1]
            return {
                "target_user": "argus",
                "target_user_present": True,
                "target_user_non_admin": (
                    account.pw_uid != 0
                    and "sudo" not in groups
                    and "wheel" not in groups
                ),
                "target_user_locked": password_field.startswith(("!", "*")),
            }
        except (
            KeyError,
            OSError,
            UnicodeError,
            StopIteration,
            subprocess.TimeoutExpired,
        ):
            return {}
    return {}

def _installed_deb_packages(names: list[str]) -> dict[str, str]:
    if platform.system().lower() != "linux":
        return {}
    if len(names) > 128 or any(not isinstance(name, str) or not _DEB_PACKAGE.fullmatch(name)
                                for name in names):
        raise AdapterError("package inventory request is invalid")
    if not names:
        return {}
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}\t${Status}\t${Version}\n", "--", *names],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError("package inventory is unavailable") from exc
    installed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] in names and parts[1] == "install ok installed":
            installed[parts[0]] = parts[2]
    return installed


def _consume_tls_private_key(path: str) -> None:
    """Remove session-visible TLS private-key material after SSLContext loads it."""
    key_path = Path(path).expanduser()
    if key_path.is_symlink():
        raise AdapterError("Capsule TLS private key cannot be a symlink")
    try:
        key_path.unlink()
    except OSError as exc:
        raise AdapterError(
            f"Capsule TLS private key could not be consumed/deleted safely: {exc}"
        ) from exc


def _require_disabled_service_start(service_name: str, start_value: int) -> None:
    """Require a Windows service registry Start value of 4 (Disabled)."""
    if int(start_value) != 4:
        raise AdapterError(
            f"Windows service {service_name!r} must be Disabled in the Capsule golden image"
        )


def _assert_powershell_direct_disabled() -> None:
    """Fail closed if the guest can still accept network-bypassing PowerShell Direct."""
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\vmicvmsession",
        ) as key:
            start_value, _kind = winreg.QueryValueEx(key, "Start")
    except OSError as exc:
        raise AdapterError(
            "cannot attest Hyper-V PowerShell Direct service policy (vmicvmsession)"
        ) from exc
    _require_disabled_service_start("vmicvmsession", int(start_value))


def _ensure_windows_target_user(
    state_store: GuestControlStateStore,
    *,
    runner=None,
) -> None:
    """Create/finalize the non-admin target user on first Capsule bootstrap."""
    if platform.system().lower() != "windows":
        return
    if state_store.load().capsule_id:
        return
    run = runner
    if run is None:
        def run(script: str) -> None:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=flags,
                check=False,
            )
            if result.returncode:
                raise AdapterError(
                    "Windows target-user initialization failed"
                )

    script = (
        "$ErrorActionPreference='Stop';"
        "$name='argus-target';"
        "$u=Get-LocalUser -Name $name -ErrorAction SilentlyContinue;"
        "if(-not $u){"
        "$plain=([guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N'));"
        "$pw=ConvertTo-SecureString $plain -AsPlainText -Force;"
        "New-LocalUser -Name $name -Password $pw -AccountNeverExpires "
        "-PasswordNeverExpires -UserMayNotChangePassword|Out-Null;"
        "};"
        "Add-LocalGroupMember -Group 'Users' -Member $name "
        "-ErrorAction SilentlyContinue;"
        "Remove-LocalGroupMember -Group 'Administrators' -Member $name "
        "-ErrorAction SilentlyContinue;"
        "$admin=Get-LocalGroupMember -Group 'Administrators' "
        "-ErrorAction Stop|Where-Object Name -Match ('\\\\'+$name+'$');"
        "if($admin){throw 'Argus target user remains administrator'}"
    )
    run(script)


class SecureGuestAgentServer(GuestAgentServer):
    def __init__(
        self,
        address,
        token: str,
        state=None,
        *,
        capsule_id: str = "",
        control_generation: int = 0,
        execution_mode: str = "",
        runtime_identity: str = "",
        control_state_store: GuestControlStateStore | None = None,
    ):
        self.token = token
        self.state = state or GuestAgentState()
        self.auth_session_id = ""
        self.capsule_id = capsule_id
        self.control_generation = int(control_generation or 0)
        self.execution_mode = execution_mode
        self.runtime_identity = runtime_identity
        self.control_state_store = control_state_store
        self.auth_lock = threading.RLock()
        ThreadingHTTPServer.__init__(self, address, SecureGuestAgentHandler)


class SecureGuestAgentHandler(GuestAgentHandler):
    server: SecureGuestAgentServer

    def _authorized(self) -> bool:
        with self.server.auth_lock:
            return super()._authorized()

    def _rotate_auth(self) -> None:
        with self.server.auth_lock:
            self._rotate_auth_locked()

    def _rotate_auth_locked(self) -> None:
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

        if self.server.control_state_store is not None:
            capsule_id = validate_capsule_id(str(body.get("capsule_id") or ""))
            try:
                generation = int(body.get("control_generation"))
            except (TypeError, ValueError) as exc:
                raise AdapterError("Capsule control generation is invalid") from exc
            execution_mode = str(body.get("execution_mode") or "")
            if (
                capsule_id != self.server.capsule_id
                or generation != self.server.control_generation
                or execution_mode != self.server.execution_mode
            ):
                raise AdapterError(
                    "auth rotation does not match the bootstrapped Capsule generation"
                )
            self.server.control_state_store.commit_generation(
                capsule_id=capsule_id,
                generation=generation,
                session_id=session_id,
                execution_mode=execution_mode,
            )

        self.server.token = token
        self.server.auth_session_id = session_id
        self._send(HTTPStatus.OK, {"ok": True, "session_id": session_id})

    def _secure_health(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        self._send(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "argus-guest-agent",
                "secure": True,
                "auth_session_id": self.server.auth_session_id,
                "capsule_id": self.server.capsule_id,
                "control_generation": self.server.control_generation,
                "execution_mode": self.server.execution_mode,
                "runtime_identity": self.server.runtime_identity,
                "guest_os": platform.system().lower(),
                "architecture": (
                    "x86_64" if platform.machine().lower() in {"amd64", "x86_64"}
                    else "aarch64" if platform.machine().lower() in {"arm64", "aarch64"}
                    else "unknown"
                ),
                "machine_identity": _machine_identity(),
                **_target_user_policy(),
                **_release_identity(),
            },
        )

    def _provisioning_packages(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        names = self._payload().get("packages")
        if not isinstance(names, list):
            raise AdapterError("package inventory request is invalid")
        self._send(HTTPStatus.OK, {
            "ok": True, "installed": _installed_deb_packages(names),
        })

    def _begin_bound_files(self) -> None:
        if not self._authorized():
            self._send(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        body = self._payload()
        session_id = validate_session_id(str(body.get("session_id") or ""))
        if self.server.auth_session_id and session_id != self.server.auth_session_id:
            raise AdapterError(
                "file workspace session does not match the rotated Capsule auth session"
            )
        data = self.server.state.begin_files(session_id)
        self._send(HTTPStatus.OK, {"ok": True, **data})

    def _dispatch(self) -> None:
        # The authority lock covers the complete privileged request, not merely
        # the bearer comparison. A generation rotation therefore happens
        # strictly before or after every authenticated operation. An older
        # bearer can never authenticate under N and continue mutating state
        # while N+1 becomes active.
        with self.server.auth_lock:
            self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        parsed = urlparse(self.path)
        try:
            if self.command == "GET" and parsed.path == "/v1/health":
                self._secure_health()
                return
            if self.command == "POST" and parsed.path == "/v1/auth/rotate":
                self._rotate_auth()
                return
            if self.command == "POST" and parsed.path == "/v1/files/begin":
                self._begin_bound_files()
                return
            if self.command == "POST" and parsed.path == "/v1/provisioning/packages":
                self._provisioning_packages()
                return
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
    parser.add_argument("--bootstrap-service", action="store_true")
    parser.add_argument("--bootstrap-root", default="")
    parser.add_argument("--runtime-identity-file", default="")
    parser.add_argument("--control-state-file", default="")
    args = parser.parse_args(argv)

    prepared_bootstrap = None
    if args.bootstrap_service:
        try:
            prepared_bootstrap = prepare_bootstrap_service(
                bootstrap_root=args.bootstrap_root or None,
                runtime_identity_file=args.runtime_identity_file or None,
                control_state_file=args.control_state_file or None,
            )
            _ensure_windows_target_user(
                prepared_bootstrap.control_state_store
            )
        except (CapsuleError, AdapterError) as exc:
            parser.error(str(exc))
        args.host = "0.0.0.0"
        args.token_file = str(prepared_bootstrap.token_path)
        args.token_env = ""
        args.tls_cert = str(prepared_bootstrap.tls_cert_path)
        args.tls_key = str(prepared_bootstrap.tls_key_path)

    if not (1 <= args.port <= 65535):
        parser.error("port must be between 1 and 65535")

    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    remote_binding = args.host not in loopback_hosts
    if remote_binding:
        try:
            _assert_powershell_direct_disabled()
        except AdapterError as exc:
            parser.error(str(exc))

    try:
        token = _consume_control_token(
            token_file=args.token_file,
            token_env=args.token_env,
        )
    except AdapterError as exc:
        parser.error(str(exc))

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

    server = SecureGuestAgentServer(
        (args.host, args.port),
        token,
        capsule_id=(
            prepared_bootstrap.manifest.capsule_id
            if prepared_bootstrap is not None else ""
        ),
        control_generation=(
            prepared_bootstrap.manifest.control_generation
            if prepared_bootstrap is not None else 0
        ),
        execution_mode=(
            prepared_bootstrap.manifest.execution_mode
            if prepared_bootstrap is not None else ""
        ),
        runtime_identity=(
            prepared_bootstrap.manifest.runtime_identity
            if prepared_bootstrap is not None else ""
        ),
        control_state_store=(
            prepared_bootstrap.control_state_store
            if prepared_bootstrap is not None else None
        ),
    )
    if has_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            context.load_cert_chain(args.tls_cert, args.tls_key)
            _consume_tls_private_key(args.tls_key)
        except (OSError, ssl.SSLError, AdapterError) as exc:
            server.server_close()
            parser.error(f"cannot initialize Capsule TLS identity: {exc}")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        if prepared_bootstrap is not None:
            prepared_bootstrap.cleanup_public_staging()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.state.close()
        finally:
            server.server_close()
            if prepared_bootstrap is not None:
                prepared_bootstrap.cleanup_all_staging()


if __name__ == "__main__":
    main()
