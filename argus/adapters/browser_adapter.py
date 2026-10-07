"""Browser adapter using Playwright — cross-platform web testing."""
from __future__ import annotations

import contextlib

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement

_OBSERVED_SELECTOR = "a, button, input, select, textarea, h1, h2, h3, [aria-label], [role]"
_MAX_ELEMENTS = 150
_PROBE_JS = """e => ({
    connected: e.isConnected,
    tag: e.tagName.toLowerCase(),
    type: e.tagName === 'INPUT' ? String(e.type || 'text').toLowerCase() : '',
    contentEditable: !!e.isContentEditable,
})"""
_FILLABLE_INPUT_TYPES = frozenset({
    "text", "password", "email", "number", "search", "tel", "url",
    "date", "time", "datetime-local", "month", "week", "color", "range",
})


class BrowserAdapter(Adapter):
    """Drives a real browser via Playwright. Requires `pip install argus-app-testing[browser]`."""

    type_name = "browser"

    def __init__(self, browser_type: str = "chromium", headless: bool = True) -> None:
        self._browser_type = browser_type
        self._headless = headless
        self._pw = None
        self._browser = None
        self._page = None
        # Observation-scoped id -> element handle map. Every element-targeted
        # action resolves through it; it is replaced wholesale by observe().
        self._elements: dict[int, object] = {}

    def capabilities(self) -> dict:
        return {
            "actions": {
                "click": {"element_id": "optional", "coordinates": True},
                "type": {"element_id": "optional"},
                "key": {},
                "navigate": {},
                "scroll": {},
                "wait": {},
                "done": {},
            },
            "notes": [
                "Prefer element_id values from the UI tree over coordinates.",
                "Key actions use Argus canonical syntax such as ctrl+s; Playwright-specific syntax is not accepted.",
            ],
        }

    def launch(self, target: str) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise AdapterError(
                "browser adapter requires playwright — "
                "install with: pip install argus-app-testing[browser] && playwright install chromium"
            )
        self._pw = sync_playwright().__enter__()
        launcher = getattr(self._pw, self._browser_type)
        self._browser = launcher.launch(headless=self._headless)
        self._page = self._browser.new_page()
        self._reset_elements()
        try:
            self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise AdapterError(f"failed to navigate to '{target}': {exc}") from exc

    def observe(self, include_screenshot: bool = True) -> Observation:
        self._reset_elements()
        if not self._page:
            return Observation(window_title="(no page)", process_alive=False)
        try:
            title = self._page.title()
            url = self._page.url
        except Exception:
            return Observation(window_title="(page error)", process_alive=False)

        screenshot = None
        if include_screenshot:
            try:
                screenshot = self._page.screenshot(type="png")
            except Exception:
                pass

        elements: list[UIElement] = []
        mapping: dict[int, object] = {}
        try:
            handles = self._page.query_selector_all(_OBSERVED_SELECTOR)
            for i, el in enumerate(handles[:_MAX_ELEMENTS]):
                try:
                    text = (el.inner_text() or el.get_attribute("aria-label") or "")[:80]
                    tag = str(el.evaluate(_PROBE_JS)["tag"]).lower()
                    box = el.bounding_box() or {}
                    rect = (
                        int(box.get("x", 0)), int(box.get("y", 0)),
                        int(box.get("x", 0) + box.get("width", 0)),
                        int(box.get("y", 0) + box.get("height", 0)),
                    )
                    elements.append(UIElement(element_id=i, control_type=tag, name=text, rect=rect))
                    mapping[i] = el
                except Exception:
                    continue
        except Exception:
            elements, mapping = [], {}
        self._elements = mapping

        return Observation(
            window_title=title,
            elements=elements,
            screenshot_png=screenshot,
            process_alive=True,
            url=url,
        )

    def _reset_elements(self) -> None:
        old, self._elements = self._elements, {}
        for handle in old.values():
            with contextlib.suppress(Exception):
                handle.dispose()

    def _resolve(self, element_id, operation: str) -> tuple[object, dict]:
        """Return the observed handle for *element_id* if it can take *operation*.

        Raises before any input is dispatched when the id is not part of the
        current observation, the node has been detached or replaced, or it
        cannot receive the requested operation.
        """
        try:
            key = int(element_id)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                f"unknown element_id {element_id!r} — re-observe and use a listed id"
            ) from exc
        handle = self._elements.get(key)
        if handle is None:
            raise AdapterError(
                f"element {key} is not in the current observation — re-observe and use a listed id"
            )
        try:
            probe = handle.evaluate(_PROBE_JS)
        except Exception:
            probe = None
        if not probe or not probe.get("connected"):
            raise AdapterError(
                f"element {key} is stale (detached by navigation or DOM replacement) — re-observe"
            )
        try:
            visible = bool(handle.is_visible())
            enabled = bool(handle.is_enabled())
        except Exception as exc:
            raise AdapterError(
                f"element {key} is stale ({type(exc).__name__}) — re-observe"
            ) from exc
        if not visible:
            raise AdapterError(f"element {key} cannot {operation}: it is not visible")
        if not enabled:
            raise AdapterError(f"element {key} cannot {operation}: it is disabled")
        if operation == "type":
            tag = probe.get("tag")
            fillable = (
                bool(probe.get("contentEditable"))
                or tag == "textarea"
                or (tag == "input" and probe.get("type") in _FILLABLE_INPUT_TYPES)
            )
            try:
                editable = fillable and bool(handle.is_editable())
            except Exception:
                editable = False
            if not editable:
                kind = f"input[type={probe.get('type')}]" if tag == "input" else tag
                raise AdapterError(f"element {key} ({kind}) is not editable — cannot type into it")
        return handle, probe

    def validate_action(self, action: dict) -> None:
        kind = (action.get("action") or "").lower()
        if self._page and kind in {"click", "type"} and "element_id" in action:
            self._resolve(action["element_id"], kind)

    def _type_into(self, element_id, text: str) -> str:
        el, probe = self._resolve(element_id, "type")
        secret = probe.get("type") == "password"
        label = f"element {element_id}" + (" (password field)" if secret else "")
        try:
            el.fill(text)
        except Exception as exc:
            if secret:
                raise AdapterError(f"type into {label} failed: {type(exc).__name__}") from None
            raise AdapterError(f"type into {label} failed: {exc}") from exc
        try:
            if probe.get("tag") in {"input", "textarea"}:
                actual = el.input_value()
            else:
                actual = el.inner_text().replace("\xa0", " ").rstrip("\n")
        except Exception as exc:
            raise AdapterError(
                f"type into {label} could not be verified: value read-back failed "
                f"({type(exc).__name__})"
            ) from None
        if _normalize(actual, probe) != _normalize(text, probe):
            if secret:
                raise AdapterError(
                    f"type into {label} did not match the intended text (value not shown)"
                )
            raise AdapterError(
                f"type into {label} did not match: intended {text[:80]!r}, "
                f"element holds {actual[:80]!r}"
            )
        if secret:
            return f"filled {label} with {len(text)} characters"
        return f"filled element {element_id} with {text!r}"

    def act(self, action: dict) -> str:
        if not self._page:
            raise AdapterError("no page loaded — call launch() first")
        kind = (action.get("action") or "").lower()

        if kind == "click":
            if "element_id" in action:
                el, _ = self._resolve(action["element_id"], "click")
                try:
                    el.click()
                except Exception as exc:
                    raise AdapterError(f"click failed: {exc}") from exc
                return f"clicked element {action['element_id']}"
            x, y = action.get("x", 0), action.get("y", 0)
            self._page.mouse.click(float(x), float(y))
            return f"clicked ({x},{y})"

        if kind == "type":
            text = str(action.get("text", ""))
            if "element_id" in action:
                return self._type_into(action["element_id"], text)
            self._page.keyboard.type(text)
            return f"typed {text!r}"

        if kind == "key":
            keys = str(action.get("keys", ""))
            self._page.keyboard.press(_to_playwright_key(keys))
            return f"pressed {keys}"

        if kind == "navigate":
            url = action.get("url", "")
            self._reset_elements()
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return f"navigated to {url}"

        if kind == "scroll":
            direction = action.get("direction", "down")
            amount = int(action.get("amount", 3)) * 100
            self._page.mouse.wheel(0, amount if direction == "down" else -amount)
            return f"scrolled {direction}"

        if kind == "wait":
            ms = min(int(float(action.get("seconds", 1)) * 1000), 30000)
            self._page.wait_for_timeout(ms)
            return f"waited {ms}ms"

        if kind == "done":
            return "done"

        raise AdapterError(f"browser adapter: unknown action '{kind}'")

    def close(self) -> None:
        try:
            if self._page:
                self._page.close()
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.__exit__(None, None, None)
        except Exception:
            pass
        self._page = self._browser = self._pw = None
        self._elements = {}


def _normalize(value: str, probe: dict) -> str:
    value = str(value).replace("\r\n", "\n")
    if probe.get("type") == "color":
        value = value.lower()
    return value


def _to_playwright_key(combo: str) -> str:
    """Translate a validated canonical Argus key chord to Playwright syntax."""
    modifiers = {"ctrl": "Control", "alt": "Alt", "shift": "Shift"}
    named = {
        "enter": "Enter",
        "tab": "Tab",
        "esc": "Escape",
        "space": "Space",
        "backspace": "Backspace",
        "delete": "Delete",
        "up": "ArrowUp",
        "down": "ArrowDown",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "home": "Home",
        "end": "End",
        "pageup": "PageUp",
        "pagedown": "PageDown",
        "insert": "Insert",
        "minus": "-",
        "equals": "=",
        "comma": ",",
        "period": ".",
        "slash": "/",
        "semicolon": ";",
        "quote": "'",
        "backquote": "`",
        "bracketleft": "[",
        "bracketright": "]",
        "backslash": "\\",
        **{f"f{i}": f"F{i}" for i in range(1, 13)},
    }
    parts = combo.split("+")
    translated = [modifiers[part] for part in parts[:-1]]
    translated.append(named.get(parts[-1], parts[-1]))
    return "+".join(translated)
