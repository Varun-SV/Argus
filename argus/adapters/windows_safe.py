"""Safe Windows UIA adapter.

This adapter deliberately avoids host-wide mouse and keyboard injection. It
operates only on UI Automation elements discovered from a process Argus can
prove belongs to the requested target. Applications that do not expose usable
UIA patterns can opt into the legacy Windows adapter explicitly through
``ARGUS_INPUT_MODE=physical``.
"""
from __future__ import annotations

import ctypes
import shlex
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set

from argus.adapters.base import AdapterError, Observation, UIElement
from argus.adapters.windows_gui import WindowsGUIAdapter, _require_pywinauto


def _require_psutil():
    try:
        import psutil

        return psutil
    except ImportError as exc:
        raise AdapterError(
            "safe Windows process ownership requires psutil — "
            "install with: pip install argus-app-testing[windows]"
        ) from exc


class SafeWindowsGUIAdapter(WindowsGUIAdapter):
    """Windows adapter restricted to semantic, target-owned UIA operations."""

    _IDENTITY_EPSILON_S = 0.01

    def __init__(self) -> None:
        super().__init__()
        self._owned_pids: Set[int] = set()
        self._owned_identities: Dict[int, float] = {}
        self._attached_pid: Optional[int] = None
        self._attached_create_time: Optional[float] = None
        # Explicit singleton attaches (for example Explorer) are observed but
        # are never lifecycle-owned/killed by Argus.
        self._owns_lifecycle = False

    def capabilities(self) -> dict:
        return {
            "actions": {
                "click": {"element_id": "required", "coordinates": False},
                "type": {"element_id": "required"},
                "wait": {},
                "done": {},
            },
            "notes": [
                "Safe Windows mode only permits direct UIA patterns on target-owned elements.",
                "click and type require an element_id; coordinates and global input are unavailable.",
            ],
        }

    # ---- verified process identity / lifecycle ---------------------------

    @classmethod
    def _same_creation_time(cls, actual: float, expected: float) -> bool:
        return abs(float(actual) - float(expected)) <= cls._IDENTITY_EPSILON_S

    def _record_process(self, proc) -> bool:
        """Remember a proven process identity without allowing PID replacement."""
        try:
            pid = int(proc.pid)
            created = float(proc.create_time())
        except Exception:
            return False

        previous = self._owned_identities.get(pid)
        if previous is not None and not self._same_creation_time(previous, created):
            # The numeric PID was recycled; never replace an identity that was
            # already part of the proven target tree.
            return False
        self._owned_identities[pid] = created
        self._owned_pids.add(pid)
        return True

    def _process_for_identity(self, pid: int, created: float, psutil_module=None):
        psutil = psutil_module or _require_psutil()
        try:
            proc = psutil.Process(int(pid))
            if not self._same_creation_time(proc.create_time(), created):
                return None
            if not proc.is_running():
                return None
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def _pid_identity_is_live(self, pid: int, psutil_module=None) -> bool:
        created = self._owned_identities.get(int(pid))
        if created is None:
            return False
        return self._process_for_identity(int(pid), created, psutil_module) is not None

    def _is_verified_owned_pid(
        self,
        pid: int,
        *,
        refresh_if_unknown: bool = False,
        psutil_module=None,
    ) -> bool:
        """Return whether *pid* still matches a pinned target process identity.

        Foreground ownership must use the same PID+creation-time proof as UI
        attachment and lifecycle cleanup. For lifecycle-owned launches only,
        an unknown PID gets one process-tree refresh so a child spawned during
        a semantic action can be proven before it is classified as third-party.
        Singleton attaches deliberately never expand trust to descendants.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False

        if self._pid_identity_is_live(pid, psutil_module):
            return True

        if (
            refresh_if_unknown
            and self._owns_lifecycle
            and pid not in self._owned_identities
        ):
            self._refresh_owned_processes(psutil_module)
            return self._pid_identity_is_live(pid, psutil_module)

        return False

    def _refresh_owned_processes(self, psutil_module=None) -> None:
        """Refresh live identities and, for owned launches, prove descendants.

        Lifecycle-owned launches may expand the verified tree because Argus
        created the root process and can prove ancestry. Explicit singleton
        attaches (notably Explorer) do *not* recursively adopt descendants:
        the Windows shell can parent unrelated user-launched applications.
        """
        psutil = psutil_module or _require_psutil()
        known = list(self._owned_identities.items())

        if self._owns_lifecycle:
            for pid, created in known:
                proc = self._process_for_identity(pid, created, psutil)
                if proc is None:
                    continue
                try:
                    children = proc.children(recursive=True)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    children = []
                for child in children:
                    self._record_process(child)

        live: Set[int] = set()
        for pid, created in self._owned_identities.items():
            if self._process_for_identity(pid, created, psutil) is not None:
                live.add(pid)
        self._owned_pids = live

    def _set_attached_identity(self, pid: int) -> None:
        created = self._owned_identities.get(int(pid))
        if created is None or not self._pid_identity_is_live(int(pid)):
            raise AdapterError(
                f"cannot attach to pid {pid}: verified process identity is no longer live"
            )
        self._attached_pid = int(pid)
        self._attached_create_time = float(created)

    def _attached_process(self, psutil_module=None):
        if self._attached_pid is None or self._attached_create_time is None:
            return None
        return self._process_for_identity(
            self._attached_pid,
            self._attached_create_time,
            psutil_module,
        )

    def _attach_verified_window(self, Application, window) -> None:
        """Attach by handle only after the window's process identity is proven."""
        pid = int(window.process_id())
        if not self._is_verified_owned_pid(pid):
            raise AdapterError(
                f"refusing window owned by pid {pid}; process identity is not verified/live"
            )

        # Connecting by an already-enumerated handle avoids pywinauto's
        # process-search wait loop. This matters for short-lived launchers:
        # descendant snapshots must continue frequently enough to prove the
        # child relationship before the launcher disappears.
        handle = int(window.handle)
        app = Application(backend="uia").connect(handle=handle)
        self._app = app
        attached = self._top_window()
        attached_pid = int(attached.process_id())
        if attached_pid != pid or not self._is_verified_owned_pid(attached_pid):
            self._app = None
            raise AdapterError(
                f"attached window identity changed from verified pid {pid} to {attached_pid}"
            )
        self._set_attached_identity(attached_pid)

    def _terminate_owned_processes(self, psutil_module=None) -> None:
        """Terminate only still-matching identities Argus itself launched."""
        if not self._owns_lifecycle:
            return
        psutil = psutil_module or _require_psutil()
        processes = []
        for pid, created in list(self._owned_identities.items()):
            proc = self._process_for_identity(pid, created, psutil)
            if proc is not None:
                processes.append(proc)

        for proc in processes:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            _gone, alive = psutil.wait_procs(processes, timeout=2.0)
        except Exception:
            alive = processes
        for proc in alive:
            expected = self._owned_identities.get(int(proc.pid))
            if expected is None:
                continue
            current = self._process_for_identity(int(proc.pid), expected, psutil)
            if current is None:
                continue
            try:
                current.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if alive:
            try:
                psutil.wait_procs(alive, timeout=2.0)
            except Exception:
                pass

    def _clear_tracking(self) -> None:
        self._app = None
        self._proc = None
        self._elements = []
        self._owned_pids.clear()
        self._owned_identities.clear()
        self._attached_pid = None
        self._attached_create_time = None
        self._owns_lifecycle = False

    def launch(self, target: str) -> None:
        """Launch and attach only when target ownership can be proven.

        Safe mode never matches unrelated desktop windows by title. A launched
        process and each accepted descendant are pinned by PID *and process
        creation time*, so launcher exit and later PID reuse cannot transfer
        authority to an unrelated process.
        """
        Application, Desktop = _require_pywinauto()
        psutil = _require_psutil()
        exe_name = shlex.split(target, posix=False)[0].lower().split("\\")[-1]

        # A singleton is an explicit attach request by executable identity. It
        # is not lifecycle-owned: close() detaches rather than killing it.
        if exe_name in self._SINGLETONS:
            try:
                self._app = Application(backend="uia").connect(path=exe_name)
                win = self._top_window()
                proc = psutil.Process(int(win.process_id()))
                if not self._record_process(proc):
                    raise AdapterError("could not pin singleton process identity")
                self._set_attached_identity(int(win.process_id()))
                self._owns_lifecycle = False
                return
            except Exception as exc:
                self._clear_tracking()
                raise AdapterError(
                    f"'{exe_name}' is a system singleton — could not attach safely: {exc}"
                ) from exc

        try:
            self._proc = subprocess.Popen(shlex.split(target, posix=False))
        except OSError as exc:
            raise AdapterError(f"could not launch '{target}': {exc}") from exc

        self._owns_lifecycle = True
        root_pid = int(self._proc.pid)
        last_err: Optional[Exception] = None
        try:
            root = psutil.Process(root_pid)
            if not self._record_process(root):
                raise AdapterError("could not pin launched process identity")
        except (psutil.NoSuchProcess, psutil.AccessDenied, AdapterError) as exc:
            last_err = exc

        # Keep proving descendants without entering a blocking PID-based UIA
        # connect. We enumerate desktop windows, but authorization is solely by
        # pinned PID+creation-time identity — never title/class matching.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and self._owned_identities:
            self._refresh_owned_processes(psutil)
            try:
                windows = Desktop(backend="uia").windows()
            except Exception as exc:
                windows = []
                last_err = exc

            for window in windows:
                try:
                    pid = int(window.process_id())
                except Exception:
                    continue
                if not self._is_verified_owned_pid(pid, psutil_module=psutil):
                    continue
                try:
                    self._attach_verified_window(Application, window)
                    return
                except Exception as exc:
                    self._app = None
                    self._attached_pid = None
                    self._attached_create_time = None
                    last_err = exc
            time.sleep(0.1)

        # Fail closed. Clean up only the identities whose ancestry/creation
        # times were proven before refusing the attach.
        try:
            self._terminate_owned_processes(psutil)
        finally:
            self._clear_tracking()
        raise AdapterError(
            f"launched '{target}' (pid {root_pid}) but could not verify a target-owned "
            f"window within 15s; refusing title-based desktop fallback. Last error: {last_err}"
        )

    def close(self) -> None:
        """Clean up the verified attached target, not merely its launcher."""
        psutil = _require_psutil()
        try:
            self._refresh_owned_processes(psutil)
            if self._owns_lifecycle and self._attached_process(psutil) is not None:
                try:
                    win = self._top_window()
                    pid = int(win.process_id())
                    if pid == self._attached_pid and self._is_verified_owned_pid(
                        pid, psutil_module=psutil
                    ):
                        win.close()
                        time.sleep(0.3)
                except Exception:
                    # Termination below is the lifecycle backstop, restricted
                    # to the pinned target identities.
                    pass
            self._terminate_owned_processes(psutil)
        finally:
            self._clear_tracking()

    def observe(self, include_screenshot: bool = True) -> Observation:
        """Observe the verified attached UI process even if its launcher exited."""
        psutil = _require_psutil()
        self._refresh_owned_processes(psutil)
        if self._attached_process(psutil) is None:
            return Observation(
                window_title="(process exited)",
                process_alive=False,
                error="verified target process exited or its process identity changed",
            )

        try:
            win = self._top_window()
            pid = int(win.process_id())
            if pid != self._attached_pid or not self._is_verified_owned_pid(
                pid, psutil_module=psutil
            ):
                raise AdapterError(
                    f"attached window identity changed from verified pid {self._attached_pid} to {pid}"
                )
            title = win.window_text()
        except Exception as exc:
            return Observation(
                window_title="(no window)",
                process_alive=self._attached_process(psutil) is not None,
                error=f"could not access verified target window: {exc}",
            )

        elements: List[UIElement] = []
        self._elements = []
        try:
            self._walk(win, elements, depth=0)
        except Exception as exc:
            return Observation(
                window_title=title,
                process_alive=True,
                error=f"UIA tree walk failed: {exc}",
            )

        screenshot: Optional[bytes] = None
        if include_screenshot:
            try:
                import io

                img = win.capture_as_image()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                screenshot = buf.getvalue()
            except Exception:
                screenshot = None

        return Observation(
            window_title=title,
            elements=elements,
            screenshot_png=screenshot,
            process_alive=True,
            dialogs=self._extra_windows(title),
        )

    def _verify_owned_top_window(self, allowed_pids: Set[int]) -> None:
        win = self._top_window()
        pid = int(win.process_id())
        if pid not in allowed_pids:
            raise AdapterError(
                f"refusing window owned by pid {pid}; verified target pids are "
                f"{sorted(allowed_pids)}"
            )
        # During ordinary operation, set membership alone is insufficient: pin
        # the creation time too so a recycled PID cannot inherit authority.
        if self._owned_identities and not self._is_verified_owned_pid(pid):
            raise AdapterError(f"refusing pid {pid}; verified process identity is no longer live")

    # ---- action policy / semantic dispatch -------------------------------

    def validate_action(self, action: dict) -> None:
        kind = action.get("action", "")
        if kind in {"click", "double_click", "right_click"}:
            if "element_id" not in action:
                raise AdapterError(
                    "coordinate input is disabled in safe Windows mode; use an element_id"
                )
            self._element(action["element_id"])
        if kind in {"double_click", "right_click", "key", "scroll", "menu"}:
            raise AdapterError(
                f"'{kind}' is not guaranteed semantic and is disabled in safe Windows mode"
            )
        if kind == "type" and "element_id" not in action:
            raise AdapterError(
                "unfocused typing is disabled in safe Windows mode; use an element_id"
            )

    @staticmethod
    def _interface(element, name: str):
        """Return a UIA pattern interface without invoking wrapper conveniences."""
        try:
            return getattr(element, name)
        except Exception:
            return None

    @staticmethod
    def _foreground_window() -> int:
        if sys.platform != "win32":
            return 0
        return int(ctypes.windll.user32.GetForegroundWindow())

    @staticmethod
    def _window_pid(hwnd: int) -> int:
        if sys.platform != "win32" or not hwnd:
            return 0
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _preserve_foreground(self, previous_hwnd: int) -> None:
        """Undo target-caused activation without fighting a user's new choice.

        Every target-owned decision is validated against the pinned
        PID+creation-time identity. If a lifecycle-owned target spawns a new
        child during the semantic action, an unknown foreground PID gets one
        ancestry refresh before Argus decides it belongs to the user/another
        application. Singleton attaches never gain descendant authority.
        """
        if sys.platform != "win32" or not previous_hwnd:
            return
        if self._is_verified_owned_pid(
            self._window_pid(previous_hwnd), refresh_if_unknown=True
        ):
            return

        user32 = ctypes.windll.user32
        deadline = time.monotonic() + 0.15
        saw_target_activation = False

        while time.monotonic() < deadline:
            current = self._foreground_window()
            if current and current != previous_hwnd:
                current_pid = self._window_pid(current)
                if not self._is_verified_owned_pid(
                    current_pid, refresh_if_unknown=True
                ):
                    # A PID that cannot be proven as the same target identity is
                    # a user/third-party choice. Never overwrite it.
                    return
                saw_target_activation = True
                if not user32.IsWindow(previous_hwnd):
                    raise AdapterError(
                        "target stole foreground focus and the previous user window no longer exists"
                    )
                user32.SetForegroundWindow(previous_hwnd)
            time.sleep(0.01)

        current = self._foreground_window()
        if current != previous_hwnd and self._is_verified_owned_pid(
            self._window_pid(current), refresh_if_unknown=True
        ):
            raise AdapterError(
                "target stole foreground focus and Argus could not restore the user's window"
            )
        if saw_target_activation and current == previous_hwnd:
            return

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()

        if kind == "click":
            element_id = action["element_id"]
            el = self._element(element_id)
            patterns = (
                ("iface_invoke", "Invoke"),
                ("iface_selection_item", "Select"),
                ("iface_toggle", "Toggle"),
            )
            errors = []
            for interface_name, method_name in patterns:
                interface = self._interface(el, interface_name)
                if interface is None:
                    continue
                method = getattr(interface, method_name, None)
                if not callable(method):
                    continue
                previous_hwnd = self._foreground_window()
                try:
                    method()
                    self._preserve_foreground(previous_hwnd)
                    return f"semantic click on element {element_id} via {method_name}"
                except AdapterError:
                    raise
                except Exception as exc:
                    errors.append(f"{method_name}: {exc}")
            detail = "; ".join(errors[-2:]) if errors else "no supported UIA pattern"
            raise AdapterError(f"element has no usable direct UIA click pattern ({detail})")

        if kind == "type":
            element_id = action["element_id"]
            text = str(action.get("text", ""))
            el = self._element(element_id)
            value = self._interface(el, "iface_value")
            set_value = getattr(value, "SetValue", None) if value is not None else None
            if not callable(set_value):
                raise AdapterError("element does not expose the UIA Value pattern")
            previous_hwnd = self._foreground_window()
            try:
                set_value(text)
                self._preserve_foreground(previous_hwnd)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(f"UIA Value.SetValue failed: {exc}") from exc
            return f"semantically set element {element_id} to {text!r} via Value.SetValue"

        if kind == "wait":
            seconds = min(float(action.get("seconds", 1.0)), 30.0)
            time.sleep(seconds)
            return f"waited {seconds}s"

        if kind == "done":
            return "done"

        raise AdapterError(
            f"safe Windows mode cannot execute '{kind}' without host-wide input"
        )
