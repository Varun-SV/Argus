"""Browser adapter using Playwright — cross-platform web testing."""
from __future__ import annotations

from typing import List, Optional

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement


class BrowserAdapter(Adapter):
    """Drives a real browser via Playwright. Requires `pip install argus-app-testing[browser]`."""

    type_name = "browser"

    def __init__(self, browser_type: str = "chromium", headless: bool = True) -> None:
        self._browser_type = browser_type
        self._headless = headless
        self._pw = None
        self._browser = None
        self._page = None

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
        try:
            self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise AdapterError(f"failed to navigate to '{target}': {exc}") from exc

    def observe(self, include_screenshot: bool = True) -> Observation:
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

        elements: List[UIElement] = []
        try:
            handles = self._page.query_selector_all(
                "a, button, input, select, textarea, h1, h2, h3, [aria-label], [role]"
            )
            for i, el in enumerate(handles[:150]):
                try:
                    text = (el.inner_text() or el.get_attribute("aria-label") or "")[:80]
                    tag = el.evaluate("e => e.tagName").lower()
                    box = el.bounding_box() or {}
                    rect = (
                        int(box.get("x", 0)), int(box.get("y", 0)),
                        int(box.get("x", 0) + box.get("width", 0)),
                        int(box.get("y", 0) + box.get("height", 0)),
                    )
                    elements.append(UIElement(element_id=i, control_type=tag, name=text, rect=rect))
                except Exception:
                    continue
        except Exception:
            pass

        return Observation(
            window_title=title,
            elements=elements,
            screenshot_png=screenshot,
            process_alive=True,
            url=url,
        )

    def act(self, action: dict) -> str:
        if not self._page:
            raise AdapterError("no page loaded — call launch() first")
        kind = (action.get("action") or "").lower()

        if kind == "click":
            if "element_id" in action:
                try:
                    handles = self._page.query_selector_all(
                        "a, button, input, select, textarea, h1, h2, h3, [aria-label], [role]"
                    )
                    el = handles[int(action["element_id"])]
                    el.click()
                    return f"clicked element {action['element_id']}"
                except Exception as exc:
                    raise AdapterError(f"click failed: {exc}") from exc
            x, y = action.get("x", 0), action.get("y", 0)
            self._page.mouse.click(float(x), float(y))
            return f"clicked ({x},{y})"

        if kind == "type":
            text = action.get("text", "")
            if "element_id" in action:
                try:
                    handles = self._page.query_selector_all(
                        "a, button, input, select, textarea, [aria-label]"
                    )
                    el = handles[int(action["element_id"])]
                    el.fill(text)
                    return f"filled element {action['element_id']} with {text!r}"
                except Exception:
                    pass
            self._page.keyboard.type(text)
            return f"typed {text!r}"

        if kind == "key":
            keys = str(action.get("keys", ""))
            self._page.keyboard.press(keys)
            return f"pressed {keys}"

        if kind == "navigate":
            url = action.get("url", "")
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
