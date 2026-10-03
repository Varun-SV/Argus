"""Adapter worker that never receives Capsule transport/authentication secrets.

The protected agent owns TLS, generation state and file-transfer authorization.
Target adapters live in a separate non-administrator process. A missing target
account/session fails closed rather than launching an app as SYSTEM/root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import sys
import threading

from argus.adapters.base import Adapter, AdapterError


_MAX_REPLY = 32 * 1024 * 1024


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--target-worker"]
    return [sys.executable, "-m", "argus.capsule.runtime_entrypoint", "--target-worker"]


def _linux_target(workspace: Path) -> tuple[int, int, dict[str, str]]:
    import grp
    import pwd

    try:
        account = pwd.getpwnam("argus")
        groups = os.getgrouplist(account.pw_name, account.pw_gid)
        names = {grp.getgrgid(gid).gr_name for gid in groups}
    except (KeyError, OSError):
        raise AdapterError("non-admin Argus target account is unavailable") from None
    if account.pw_uid == 0 or names.intersection({"sudo", "wheel", "root", "docker", "lxd", "incus", "libvirt"}):
        raise AdapterError("Argus target account must not be privileged")
    # Transfers finish before the worker starts. Never follow target-created
    # symlinks while authorizing a subsequent launch.
    _authorize_linux_workspace(workspace, account.pw_uid, account.pw_gid)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": account.pw_dir,
        "USER": account.pw_name, "LOGNAME": account.pw_name,
        "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
    }
    # GUI adapters require an actual target-user desktop. An X11 socket alone
    # is not sufficient authority; the worker must possess the user's cookie.
    authority = Path(account.pw_dir) / ".Xauthority"
    gdm_authority = Path(env["XDG_RUNTIME_DIR"]) / "gdm" / "Xauthority"
    if gdm_authority.is_file() and gdm_authority.stat().st_uid == account.pw_uid:
        authority = gdm_authority
    if authority.is_file() and authority.stat().st_uid == account.pw_uid:
        env.update(DISPLAY=":0", XAUTHORITY=str(authority))
    return account.pw_uid, account.pw_gid, env


def _authorize_linux_workspace(workspace: Path, uid: int, gid: int) -> None:
    # Bind every chown to an opened object. A target-created directory swap
    # cannot redirect a privileged recursive walk through an outside symlink.
    def authorize(fd):
        info = os.fstat(fd)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise AdapterError("target workspace must contain only regular files and directories")
        os.fchown(fd, uid, gid)
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(fd) as entries:
                for entry in entries:
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=fd)
                    try:
                        authorize(child)
                    finally:
                        os.close(child)

    fd = None
    try:
        fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        authorize(fd)
    except OSError:
        raise AdapterError("target workspace ownership cannot be established safely") from None
    finally:
        if fd is not None:
            os.close(fd)


class _WindowsProcess:
    """Pipe-backed child created with the existing target desktop's token."""

    def __init__(self, command: list[str], workspace: Path):
        try:
            import msvcrt
            import win32api
            import win32con
            import win32event
            import win32job
            import win32pipe
            import win32process
            import win32profile
            import win32security
            import win32ts
            from ctypes import windll
            from ctypes import wintypes
        except ImportError:
            raise AdapterError("Windows guest runtime requires offline pywin32 support") from None
        self._api, self._event, self._process = win32api, win32event, win32process
        token = None
        handles = []
        try:
            session = windll.kernel32.WTSGetActiveConsoleSessionId()
            token = win32ts.WTSQueryUserToken(session)
            target_sid, _, _ = win32security.LookupAccountName(None, "argus-target")
            user_sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            admin_sid = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
            groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
            if user_sid != target_sid or any(sid == admin_sid for sid, _ in groups):
                raise AdapterError("an interactive non-admin argus-target session is required")
            from argus.capsule.permissions import _protect_windows_directory

            for path in (workspace, *workspace.rglob("*")):
                if path.is_symlink():
                    raise AdapterError("target workspace contains a symlink")
                _protect_windows_directory(path, extra_sid=win32security.ConvertSidToStringSid(target_sid))
            attributes = win32security.SECURITY_ATTRIBUTES()
            attributes.bInheritHandle = True
            child_in, parent_in = win32pipe.CreatePipe(attributes, 0)
            parent_out, child_out = win32pipe.CreatePipe(attributes, 0)
            handles.extend((child_in, parent_in, parent_out, child_out))
            windll.kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
            for handle in (parent_in, parent_out):
                if not windll.kernel32.SetHandleInformation(int(handle), win32con.HANDLE_FLAG_INHERIT, 0):
                    raise AdapterError("cannot protect target worker pipe handles")
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput = child_in, child_out
            # The worker redirects diagnostic output away from this pipe.
            startup.hStdError = child_out
            startup.lpDesktop = "winsta0\\default"
            profile_env = win32profile.CreateEnvironmentBlock(token, False)
            allowed = {"systemroot", "windir", "path", "userprofile", "appdata", "localappdata",
                       "homedrive", "homepath", "username", "userdomain", "temp", "tmp",
                       "programfiles", "programfiles(x86)", "programdata"}
            env = {name: value for name, value in profile_env.items() if name.lower() in allowed}
            self._job = win32job.CreateJobObject(None, None)
            limits = win32job.QueryInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self._job, win32job.JobObjectExtendedLimitInformation, limits)
            handles.append(self._job)
            self._handle, thread, _pid, _tid = win32process.CreateProcessAsUser(
                token, command[0], subprocess.list2cmdline(command), None, None, True,
                win32con.CREATE_NO_WINDOW | win32con.CREATE_SUSPENDED, env, str(workspace), startup,
            )
            handles.extend((self._handle, thread))
            win32job.AssignProcessToJobObject(self._job, self._handle)
            win32process.ResumeThread(thread)
            thread.Close()
            child_in.Close()
            child_out.Close()
            self.stdin = os.fdopen(msvcrt.open_osfhandle(parent_in.Detach(), os.O_WRONLY), "wb", buffering=0)
            self.stdout = os.fdopen(msvcrt.open_osfhandle(parent_out.Detach(), os.O_RDONLY), "rb", buffering=0)
            handles.clear()
        except Exception:
            if hasattr(self, "_handle"):
                try:
                    win32process.TerminateProcess(self._handle, 1)
                except Exception:
                    pass
            for handle in handles:
                try:
                    handle.Close()
                except Exception:
                    pass
            raise AdapterError("cannot start a worker in the non-admin target desktop") from None
        finally:
            if token is not None:
                token.Close()

    def kill(self) -> None:
        self._job.Close()  # Fences the worker and all of its descendants.

    def wait(self, timeout: float = 5) -> None:
        if self._event.WaitForSingleObject(self._handle, int(timeout * 1000)) == 258:
            raise subprocess.TimeoutExpired("target worker", timeout)
        self._handle.Close()


class TargetWorkerAdapter(Adapter):
    """Adapter RPC over private pipes to a non-admin target-user process."""

    def __init__(self, adapter_type: str, execution_mode: str):
        if execution_mode not in {"isolated", "shared_user"}:
            raise AdapterError("invalid target execution mode")
        self.type_name = adapter_type
        self.execution_mode = execution_mode
        self._workspace = None
        self._child = None
        self._replies = queue.Queue(maxsize=1)
        self._capabilities = {}

    def set_working_directory(self, path: str) -> None:
        self._workspace = Path(path).resolve(strict=True)

    def _reader(self, child, replies) -> None:
        try:
            while True:
                raw = child.stdout.readline(_MAX_REPLY + 1)
                if not raw or len(raw) > _MAX_REPLY:
                    break
                replies.put(json.loads(raw), timeout=1)
        except Exception:
            pass
        try:
            replies.put(None, timeout=1)
        except queue.Full:
            pass

    def _rpc(self, operation: str, *, timeout_seconds: float = 75, **arguments):
        try:
            body = json.dumps({"operation": operation, **arguments}).encode() + b"\n"
            self._child.stdin.write(body)
            self._child.stdin.flush()
            reply = self._replies.get(timeout=timeout_seconds)
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                raise AdapterError("target worker operation failed")
            return reply["result"]
        except (OSError, ValueError, queue.Empty, AttributeError, KeyError):
            raise AdapterError("target worker reply is unavailable or invalid") from None

    def _launch(self, target: str, literal: bool) -> None:
        if self._workspace is None:
            raise AdapterError("target worker requires an initialized file workspace")
        self._replies = queue.Queue(maxsize=1)
        if os.name == "nt":
            self._child = _WindowsProcess(_worker_command(), self._workspace)
        else:
            uid, gid, env = _linux_target(self._workspace)
            self._child = subprocess.Popen(
                _worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=self._workspace, env=env,
                user=uid, group=gid, extra_groups=(), close_fds=True,
                start_new_session=True,
            )
        threading.Thread(target=self._reader, args=(self._child, self._replies), daemon=True).start()
        try:
            self._capabilities = self._rpc(
                "start", adapter_type=self.type_name, target=target,
                literal_target=literal, input_mode=os.environ.get("ARGUS_INPUT_MODE", "safe"),
                workspace=str(self._workspace), execution_mode=self.execution_mode,
            )
        except Exception:
            self.close()
            raise

    def launch(self, target: str) -> None:
        self._launch(target, False)

    def launch_literal(self, target: str) -> None:
        self._launch(target, True)

    def capabilities(self) -> dict:
        return self._capabilities

    def observe(self, include_screenshot: bool = True):
        from argus.capsule.guest import _observation_from_dict

        return _observation_from_dict(self._rpc("observe", include_screenshot=include_screenshot))

    def prepare_action(self, action: dict) -> dict:
        return self._rpc("prepare", action=action)

    def dispatch_prepared_action(self, action: dict) -> str:
        return self._rpc("dispatch", action=action)

    def act(self, action: dict) -> str:
        return self.dispatch_prepared_action(self.prepare_action(action))

    def close(self) -> None:
        if self._child is None:
            return
        try:
            try:
                self._rpc("close", timeout_seconds=2)
            except AdapterError:
                pass
            # Closing cannot wait indefinitely behind a stalled application.
            try:
                if os.name == "nt":
                    self._child.kill()
                else:
                    os.killpg(self._child.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self._child.wait(timeout=5)
            finally:
                self._child.stdin.close()
                self._child.stdout.close()
                self._child = None
        except subprocess.TimeoutExpired:
            raise AdapterError("target worker teardown is uncertain") from None


def worker_main() -> None:
    """No control endpoint, generation registry, bearer or TLS code in worker."""
    # Keep RPC on private non-inheritable duplicates, then replace all OS-level
    # std handles with NUL before any adapter can launch a tested application.
    # Python sys.stdout redirection alone does not stop Popen children from
    # inheriting the original protocol pipes.
    requests = os.fdopen(os.dup(sys.stdin.fileno()), "r", encoding="utf-8")
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.set_inheritable(requests.fileno(), False)
    os.set_inheritable(protocol.fileno(), False)
    null = os.open(os.devnull, os.O_RDWR)
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null, descriptor)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel = ctypes.windll.kernel32
            kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
            for selector, descriptor in ((-10, 0), (-11, 1), (-12, 2)):
                if not kernel.SetStdHandle(selector & 0xffffffff, msvcrt.get_osfhandle(descriptor)):
                    raise AdapterError("cannot isolate target worker standard handles")
    finally:
        os.close(null)
    from argus.adapters.base import create_adapter
    from argus.capsule.guest_agent import _observation_to_dict

    sys.stdout = sys.stderr
    adapter = None
    prepared = None
    try:
        for line in requests:
            try:
                request = json.loads(line)
                operation = request["operation"]
                if operation == "start":
                    if adapter is not None:
                        raise AdapterError("target worker is already started")
                    if request.get("execution_mode") not in {"isolated", "shared_user"}:
                        raise AdapterError("target worker execution mode is invalid")
                    if os.name == "posix" and os.geteuid() == 0:
                        raise AdapterError("target worker must not run as root")
                    if sys.platform.startswith("linux"):
                        import ctypes

                        if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0):
                            raise AdapterError("cannot establish non-admin worker policy")
                    os.environ["ARGUS_INPUT_MODE"] = request["input_mode"]
                    adapter = create_adapter(request["adapter_type"])
                    setter = getattr(adapter, "set_working_directory", None)
                    if callable(setter):
                        setter(request["workspace"])
                    if request["literal_target"]:
                        adapter.launch_literal(request["target"])
                    else:
                        adapter.launch(request["target"])
                    result = adapter.capabilities()
                elif operation == "close":
                    if adapter is not None:
                        adapter.close()
                    protocol.write(json.dumps({"ok": True, "result": None}) + "\n")
                    protocol.flush()
                    return
                elif adapter is None:
                    raise AdapterError("target worker has no adapter")
                elif operation == "observe":
                    result = _observation_to_dict(adapter.observe(request["include_screenshot"]))
                elif operation == "prepare":
                    if prepared is not None:
                        raise AdapterError("an action is already prepared")
                    prepared = adapter.prepare_action(request["action"])
                    result = prepared
                elif operation == "dispatch":
                    if prepared is None or request["action"] != prepared:
                        raise AdapterError("dispatch does not match the prepared action")
                    action, prepared = prepared, None
                    result = adapter.dispatch_prepared_action(action)
                else:
                    raise AdapterError("unsupported target worker operation")
                reply = {"ok": True, "result": result}
            except Exception:
                # Worker/app stderr may contain arbitrary sensitive output.
                reply = {"ok": False}
            protocol.write(json.dumps(reply) + "\n")
            protocol.flush()
    finally:
        if adapter is not None:
            adapter.close()
        requests.close()
        protocol.close()
