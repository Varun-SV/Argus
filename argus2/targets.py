"""The things Argus can drive: a command line and a web page.

Both expose the same small surface to the engine:

* ``start()`` / ``close()``
* ``observe()``  -> what a user would see right now (text, optional screenshot)
* ``act(action)`` -> perform one action, return a one-line result
* ``check(name, expected)`` -> exact, model-free assertion
* ``ACTIONS`` -> the action list shown to the model
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class TargetError(Exception):
    """An action could not be performed (the model may try something else)."""


class SetupError(Exception):
    """The target cannot be driven at all (missing browser, bad command …)."""


@dataclass
class Observation:
    text: str
    screenshot: Optional[bytes] = None  # JPEG
    problems: List[str] = field(default_factory=list)  # e.g. JavaScript errors


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-_]|\r(?!\n)")


def _clean(text: str) -> str:
    return _ANSI.sub("", text).replace("\r\n", "\n")


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…(earlier output cut)…\n" + text[-limit:]


def _number(action: dict, key: str, default: float, low: float, high: float) -> float:
    value = action.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetError(f"'{key}' must be a number.")
    return max(low, min(high, float(value)))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

class CliTarget:
    kind = "cli"

    def __init__(self, target: Dict[str, Any], workspace: Path) -> None:
        self.command = target.get("command") or ""
        cwd = target.get("cwd") or ""
        self.cwd = (workspace / cwd).resolve() if cwd else workspace
        self.transcript = ""
        self.last_output = ""
        self.last_exit: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self._master: Optional[int] = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._stamp = time.monotonic()

    @property
    def ACTIONS(self) -> str:
        lines = ['{"action":"run","command":"<one shell command>"}  run a command and wait for it to finish']
        if self.command:
            lines.append('{"action":"send","text":"<a line of input>"}  type a line into the open program '
                         f"({self.command}) and press Enter")
        lines.append('{"action":"wait","seconds":1}  wait for more output')
        return "\n".join(lines)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self.cwd.is_dir():
            raise SetupError(f"The folder to run in does not exist: {self.cwd}")
        if not self.command:
            return
        try:
            if os.name == "posix":
                import pty
                self._master, slave = pty.openpty()
                try:
                    self._proc = subprocess.Popen(
                        self.command, shell=True, cwd=str(self.cwd), stdin=slave, stdout=slave,
                        stderr=slave, start_new_session=True, close_fds=True,
                        env={**os.environ, "TERM": "dumb", "NO_COLOR": "1"})
                finally:
                    os.close(slave)
            else:
                self._proc = subprocess.Popen(
                    self.command, shell=True, cwd=str(self.cwd), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as exc:
            raise SetupError(f"Could not start '{self.command}': {exc}") from None
        threading.Thread(target=self._reader, daemon=True).start()
        self.last_output = self._settle(1.5, 5.0)
        if self._proc.poll() is not None and self._proc.returncode != 0:
            self.last_exit = self._proc.returncode
            raise SetupError(f"'{self.command}' exited straight away with code {self._proc.returncode}."
                             + (f"\n{_tail(self.last_output, 600)}" if self.last_output.strip() else ""))

    def _reader(self) -> None:
        proc = self._proc
        while True:
            try:
                if self._master is not None:
                    chunk = os.read(self._master, 4096)
                else:
                    chunk = proc.stdout.read1(4096)  # type: ignore[union-attr]
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._buffer += chunk.decode("utf-8", "replace")
                self._stamp = time.monotonic()

    def _settle(self, quiet: float, limit: float) -> str:
        """Collect output until the program has been quiet for a moment."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self._lock:
                idle = time.monotonic() - self._stamp
                has_output = bool(self._buffer)
            exited = self._proc is not None and self._proc.poll() is not None
            if (has_output and idle >= quiet) or (exited and idle >= 0.2):
                break
        with self._lock:
            out, self._buffer = _clean(self._buffer), ""
        self.transcript += out
        if self._proc is not None and self._proc.poll() is not None:
            self.last_exit = self._proc.returncode
        return out

    def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=10)
                proc.wait(timeout=3)
            except Exception:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    pass
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None

    # -- driving ---------------------------------------------------------
    def observe(self, screenshot: bool = False) -> Observation:
        parts = [f"Working folder: {self.cwd}"]
        if self.command:
            alive = self._proc is not None and self._proc.poll() is None
            parts.append(f"Open program: {self.command} ({'running' if alive else f'exited with code {self.last_exit}'})")
        if self.last_exit is not None and not self.command:
            parts.append(f"Exit code of the last command: {self.last_exit}")
        parts.append("Output of the last action:\n" + (_tail(self.last_output, 4000) or "(no output)"))
        return Observation("\n".join(parts))

    def act(self, action: dict) -> str:
        kind = action.get("action")
        if kind == "run":
            command = action.get("command")
            if not isinstance(command, str) or not command.strip():
                raise TargetError("'run' needs a command.")
            return self._run(command, _number(action, "timeout", 60, 1, 3600))
        if kind == "send":
            if self._proc is None:
                raise TargetError("There is no open program to type into; use 'run'.")
            if self._proc.poll() is not None:
                raise TargetError(f"The program has exited (code {self._proc.returncode}).")
            text = action.get("text")
            if not isinstance(text, str):
                raise TargetError("'send' needs text.")
            data = (text + "\n").encode("utf-8")
            try:
                if self._master is not None:
                    os.write(self._master, data)
                else:
                    self._proc.stdin.write(data)  # type: ignore[union-attr]
                    self._proc.stdin.flush()  # type: ignore[union-attr]
            except OSError as exc:
                raise TargetError(f"Could not type into the program: {exc}") from None
            self.transcript += "" if self._master is not None else f"> {text}\n"
            self.last_output = self._settle(0.4, 10.0)
            return f"typed {text!r}; the program printed {len(self.last_output)} characters"
        if kind == "wait":
            seconds = _number(action, "seconds", 1, 0, 60)
            time.sleep(seconds)
            if self._proc is not None:
                self.last_output = self._settle(0.2, 1.0)
            return f"waited {seconds:g}s"
        raise TargetError(f"Unknown action '{kind}' for a command-line target.")

    def _run(self, command: str, timeout: float) -> str:
        self.transcript += f"$ {command}\n"
        try:
            done = subprocess.run(command, shell=True, cwd=str(self.cwd), capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            partial = _clean(((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace"))
            self.last_output, self.last_exit = partial, None
            self.transcript += partial
            raise TargetError(f"The command did not finish within {timeout:g}s.") from None
        except OSError as exc:
            raise TargetError(f"Could not run the command: {exc}") from None
        self.last_output = _clean((done.stdout + done.stderr).decode("utf-8", "replace"))
        self.last_exit = done.returncode
        self.transcript += self.last_output + ("" if self.last_output.endswith("\n") or not self.last_output else "\n")
        return f"exit code {done.returncode}, {len(self.last_output)} characters of output"

    def check(self, name: str, expected: Any) -> Tuple[bool, str]:
        if name == "exit_code":
            return self.last_exit == expected, f"exit code was {self.last_exit}, expected {expected}"
        if name == "output_contains":
            return expected in self.last_output, f"last output {'contains' if expected in self.last_output else 'does not contain'} {expected!r}"
        if name == "output_absent":
            return expected not in self.last_output, f"last output {'contains' if expected in self.last_output else 'does not contain'} {expected!r}"
        if name == "output_matches":
            found = re.search(expected, self.last_output, re.M) is not None
            return found, f"last output {'matches' if found else 'does not match'} /{expected}/"
        if name == "transcript_contains":
            found = expected in self.transcript
            return found, f"the session so far {'contains' if found else 'does not contain'} {expected!r}"
        return False, f"unknown check '{name}'"


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-argus-ref]').forEach(e => e.removeAttribute('data-argus-ref'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],' +
    '[role=checkbox],[role=radio],[role=menuitem],[role=option],[role=switch],[role=combobox],' +
    '[contenteditable=""],[contenteditable=true],[onclick]';
  const clean = s => (s || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const out = [];
  let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    if (n >= 150) break;
    if (e.type === 'hidden') continue;
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || s.visibility === 'hidden' || s.display === 'none') continue;
    n += 1;
    e.setAttribute('data-argus-ref', String(n));
    const tag = e.tagName.toLowerCase();
    let label = e.getAttribute('aria-label') || '';
    if (!label && e.id) {
      const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
      if (l) label = l.innerText;
    }
    if (!label && e.closest('label')) label = e.closest('label').innerText;
    if (!label && tag !== 'input' && tag !== 'textarea' && tag !== 'select') label = e.innerText;
    if (!label) label = e.placeholder || e.name || e.title || e.alt || (['submit', 'button'].includes(e.type) ? e.value : '') || '';
    const item = {ref: n, tag: tag, label: clean(label)};
    if (tag === 'input') item.type = e.type || 'text';
    if (tag === 'input' || tag === 'textarea') {
      if (e.type === 'checkbox' || e.type === 'radio') item.checked = e.checked;
      else if (e.type === 'password') item.value = e.value ? '(hidden)' : '';
      else if (!['submit', 'button'].includes(e.type)) item.value = clean(e.value);
    }
    if (tag === 'select') {
      item.value = clean(e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : '');
      item.options = Array.from(e.options).slice(0, 12).map(o => clean(o.text));
    }
    if (e.disabled || e.getAttribute('aria-disabled') === 'true') item.disabled = true;
    out.push(item);
  }
  return {elements: out, text: document.body ? document.body.innerText.slice(0, 5000) : ''};
}
"""


def find_system_browser() -> str:
    """Name of an installed Chrome/Edge channel Playwright can drive, if any."""
    candidates = {
        "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
        "msedge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
    }
    paths = {
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    }
    for channel in ("chrome", "msedge"):
        if any(shutil.which(name) for name in candidates[channel]) or any(os.path.exists(p) for p in paths[channel]):
            return channel
    return ""


def browser_status() -> Dict[str, Any]:
    """Can a browser be driven on this computer? (No browser is started.)"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "detail": "Web testing needs the 'playwright' package: pip install playwright",
                "can_install": False}
    bundled = ""
    try:
        with sync_playwright() as p:
            bundled = p.chromium.executable_path
    except Exception:
        pass
    if bundled and os.path.exists(bundled):
        return {"ok": True, "detail": "Using Argus's own Chromium.", "can_install": False}
    channel = find_system_browser()
    if channel:
        name = "Google Chrome" if channel == "chrome" else "Microsoft Edge"
        return {"ok": True, "detail": f"Using {name} installed on this computer.", "can_install": True}
    return {"ok": False, "detail": "No browser found. Install Chromium for Argus, or install Chrome or Edge.",
            "can_install": True}


def install_browser() -> Tuple[bool, str]:
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        driver = compute_driver_executable()
        command = list(driver) if isinstance(driver, (tuple, list)) else [str(driver)]
        done = subprocess.run(command + ["install", "chromium"], capture_output=True, text=True,
                              timeout=900, env=get_driver_env())
    except ImportError:
        return False, "The 'playwright' package is not installed."
    except subprocess.TimeoutExpired:
        return False, "The download took too long. Check the internet connection and try again."
    except OSError as exc:
        return False, f"Could not start the installer: {exc}"
    output = (done.stdout + done.stderr).strip()
    return done.returncode == 0, output[-1500:] or ("Chromium installed." if done.returncode == 0 else "Install failed.")


class WebTarget:
    kind = "web"
    ACTIONS = "\n".join([
        '{"action":"click","ref":3}  click the element numbered [3]',
        '{"action":"type","ref":2,"text":"hello","submit":false}  replace the content of a text box; '
        '"submit":true also presses Enter',
        '{"action":"select","ref":4,"option":"<option text>"}  choose an option in a drop-down',
        '{"action":"press","key":"Enter"}  press a key (Enter, Tab, Escape, ArrowDown, …)',
        '{"action":"scroll","direction":"down"}  scroll the page up or down',
        '{"action":"navigate","url":"https://…"}  open another address',
        '{"action":"back"}  go back one page',
        '{"action":"wait","seconds":1}  wait for the page to change',
    ])

    def __init__(self, target: Dict[str, Any], headless: bool = True) -> None:
        self.url = target["url"]
        self.headless = headless
        self._pw = None
        self._browser = None
        self.page = None
        self._problems: List[str] = []
        self._dialogs: List[str] = []
        self._labels: Dict[int, str] = {}
        self.browser_name = ""

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SetupError("Web testing needs the 'playwright' package. Install it with: "
                             "pip install playwright") from None
        self._pw = sync_playwright().start()
        errors = []
        for channel in (None, "chrome", "msedge"):
            try:
                options: Dict[str, Any] = {"headless": self.headless}
                if channel:
                    options["channel"] = channel
                self._browser = self._pw.chromium.launch(**options)
                self.browser_name = channel or "chromium"
                break
            except Exception as exc:
                errors.append(str(exc).splitlines()[0][:160])
        if self._browser is None:
            self.close()
            raise SetupError("No browser could be started. Open Settings → Browser and install Chromium, "
                             "or install Google Chrome or Microsoft Edge. (" + errors[0] + ")")
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        context.set_default_timeout(8000)
        self.page = context.new_page()
        self.page.on("pageerror", lambda exc: self._problem(f"JavaScript error: {str(exc)[:300]}"))
        self.page.on("console", lambda msg: self._problem(f"Console error: {msg.text[:300]}")
                     if msg.type == "error" else None)
        self.page.on("response", lambda r: self._problem(f"HTTP {r.status} from {r.url[:200]}")
                     if r.status >= 500 else None)
        self.page.on("dialog", self._dialog)
        self._goto(self.url, first=True)

    def _problem(self, text: str) -> None:
        if text not in self._problems and len(self._problems) < 50:
            self._problems.append(text)

    def _dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message[:300]}")
        try:
            dialog.accept()
        except Exception:
            pass

    def _goto(self, url: str, first: bool = False) -> None:
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            reason = str(exc).splitlines()[0].replace("Page.goto:", "").strip(" :")[:200]
            message = f"Could not open {url} ({reason}). Check the address and the network connection."
            if first:
                self.close()
                raise SetupError(message) from None
            raise TargetError(message) from None
        self._calm()

    def _calm(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        time.sleep(0.35)

    def close(self) -> None:
        # Stop the driver as well: a leaked driver blocks the next launch on this thread.
        for closer in (lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._browser = self._pw = self.page = None

    # -- driving ---------------------------------------------------------
    def screenshot(self) -> Optional[bytes]:
        try:
            return self.page.screenshot(type="jpeg", quality=55)
        except Exception:
            return None

    def observe(self, screenshot: bool = False) -> Observation:
        try:
            snap = self.page.evaluate(_SNAPSHOT_JS)
            title = self.page.title()
        except Exception as exc:  # mid-navigation: wait once and retry
            self._calm()
            try:
                snap = self.page.evaluate(_SNAPSHOT_JS)
                title = self.page.title()
            except Exception:
                raise TargetError(f"The page could not be read: {str(exc).splitlines()[0][:200]}") from None
        lines = [f"Address: {self.page.url}", f"Title: {title}"]
        if self._dialogs:
            lines.append("Pop-up messages shown (already dismissed): " + " | ".join(self._dialogs))
            self._dialogs = []
        lines.append("Things you can interact with:")
        self._labels = {el["ref"]: el["label"] or el["tag"] for el in snap["elements"]}
        for el in snap["elements"]:
            kind = el["tag"] + (f"({el['type']})" if el.get("type") else "")
            line = f"[{el['ref']}] {kind} \"{el['label']}\""
            if "value" in el:
                line += f" value=\"{el['value']}\""
            if "checked" in el:
                line += " checked" if el["checked"] else " unchecked"
            if el.get("options"):
                line += " options=" + ", ".join(el["options"])
            if el.get("disabled"):
                line += " (disabled)"
            lines.append(line)
        if not snap["elements"]:
            lines.append("(nothing)")
        lines.append("Text on the page:\n" + (snap["text"].strip() or "(empty)"))
        problems, self._problems = self._problems, []
        if problems:
            lines.append("Problems the browser reported:\n" + "\n".join(problems))
        return Observation("\n".join(lines), self.screenshot() if screenshot else None, problems)

    def name_of(self, ref: Any) -> str:
        """How a person would refer to element [ref], e.g. ``"Sign in" [3]``."""
        label = self._labels.get(ref) if isinstance(ref, int) else None
        return f'"{label}" [{ref}]' if label else f"[{ref}]"

    def _element(self, action: dict):
        ref = action.get("ref")
        if isinstance(ref, bool) or not isinstance(ref, int):
            raise TargetError("This action needs 'ref': the number shown in [brackets].")
        locator = self.page.locator(f'[data-argus-ref="{ref}"]')
        if locator.count() != 1:
            raise TargetError(f"There is no element [{ref}] on the page right now. Look at the current list.")
        return locator, ref

    def act(self, action: dict) -> str:
        kind = action.get("action")
        try:
            return self._act(kind, action)
        except TargetError:
            raise
        except Exception as exc:
            raise TargetError(f"'{kind}' failed: {str(exc).splitlines()[0][:240]}") from None

    def _act(self, kind: Any, action: dict) -> str:
        page = self.page
        if kind == "click":
            locator, ref = self._element(action)
            locator.click()
            self._calm()
            return f"clicked [{ref}]"
        if kind == "type":
            locator, ref = self._element(action)
            text = action.get("text")
            if not isinstance(text, str):
                raise TargetError("'type' needs text.")
            locator.fill(text)
            # Read back, so a "typed" result always means the text is really there.
            actual = locator.evaluate("e => 'value' in e ? e.value : e.innerText")
            if (actual or "").strip() != text.strip():
                raise TargetError(f"Typed into [{ref}] but it now contains {actual!r} instead of {text!r}.")
            if action.get("submit"):
                locator.press("Enter")
                self._calm()
                return f"typed {text!r} into [{ref}] and pressed Enter"
            return f"typed {text!r} into [{ref}]"
        if kind == "select":
            locator, ref = self._element(action)
            option = action.get("option")
            if not isinstance(option, str):
                raise TargetError("'select' needs the option text.")
            locator.select_option(label=option)
            self._calm()
            return f"selected {option!r} in [{ref}]"
        if kind == "press":
            key = action.get("key")
            if not isinstance(key, str) or not key:
                raise TargetError("'press' needs a key name.")
            page.keyboard.press(key)
            self._calm()
            return f"pressed {key}"
        if kind == "scroll":
            down = str(action.get("direction", "down")).lower() != "up"
            page.mouse.wheel(0, 600 if down else -600)
            time.sleep(0.3)
            return f"scrolled {'down' if down else 'up'}"
        if kind == "navigate":
            url = action.get("url")
            if not isinstance(url, str) or not re.match(r"^(https?|file)://", url, re.I):
                raise TargetError("'navigate' needs a full address starting with http:// or https://")
            self._goto(url)
            return f"opened {url}"
        if kind == "back":
            page.go_back(wait_until="domcontentloaded")
            self._calm()
            return "went back"
        if kind == "wait":
            seconds = _number(action, "seconds", 1, 0, 30)
            time.sleep(seconds)
            return f"waited {seconds:g}s"
        # Exact steps written in a test file (Playwright selectors).
        if kind == "goto":
            self._goto(action["url"])
            return f"opened {action['url']}"
        if kind == "click_selector":
            page.locator(action["selector"]).first.click()
            self._calm()
            return f"clicked {action['selector']}"
        if kind == "fill_selector":
            page.locator(action["selector"]).first.fill(action["text"])
            return f"filled {action['selector']}"
        raise TargetError(f"Unknown action '{kind}' for a web page.")

    def check(self, name: str, expected: Any) -> Tuple[bool, str]:
        page = self.page
        positive = name in ("text_visible", "url_contains", "title_contains", "element_visible")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if name in ("text_visible", "text_absent"):
                    body = page.evaluate(
                        "() => (document.body ? document.body.innerText : '') + '\\n' + "
                        "Array.from(document.querySelectorAll('input:not([type=password]),textarea'))"
                        ".map(e => e.value).join('\\n')")
                    present = expected in body
                    ok = present if name == "text_visible" else not present
                    detail = f"the page {'shows' if present else 'does not show'} {expected!r}"
                elif name == "url_contains":
                    ok = expected in page.url
                    detail = f"the address is {page.url}"
                elif name == "title_contains":
                    title = page.title()
                    ok = expected in title
                    detail = f"the title is {title!r}"
                elif name in ("element_visible", "element_absent"):
                    locator = page.locator(expected)
                    visible = locator.count() > 0 and locator.first.is_visible()
                    ok = visible if name == "element_visible" else not visible
                    detail = f"{expected} is {'visible' if visible else 'not visible'}"
                else:
                    return False, f"unknown check '{name}'"
            except Exception as exc:
                ok, detail = False, f"could not check: {str(exc).splitlines()[0][:200]}"
            # Positive checks wait a little for the page to catch up; negative ones must hold.
            if ok or not positive or time.monotonic() >= deadline:
                return ok, detail
            time.sleep(0.25)


def make_target(target: Dict[str, Any], workspace: Path, headless: bool = True):
    if target["type"] == "web":
        return WebTarget(target, headless=headless)
    return CliTarget(target, workspace)


def python_command() -> str:
    """A command that starts this Python (handy for samples and tests)."""
    exe = sys.executable
    return f'"{exe}"' if " " in exe else exe
