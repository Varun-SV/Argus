"""Browser element-targeted actions resolve through one observation-scoped map.

The unit layer drives :class:`BrowserAdapter` with fake page/handle objects so
the mapping, pre-dispatch rejection and read-back rules are covered without a
browser. The fixture layer repeats the key scenarios against real Chromium in
a fresh subprocess per test and skips cleanly when no browser can launch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from argus.adapters.base import AdapterError, PolicyAdapter
from argus.adapters.browser_adapter import BrowserAdapter

_PROBE_TAG_ONLY = "e => e.tagName"
_FILLABLE = {
    "", "text", "password", "email", "number", "search", "tel", "url",
    "date", "time", "datetime-local", "month", "week", "color", "range",
}


class FakeHandle:
    def __init__(
        self,
        tag,
        *,
        text="",
        aria_label=None,
        role=None,
        input_type=None,
        box=(0, 0, 10, 10),
        visible=True,
        enabled=True,
        readonly=False,
        content_editable=False,
        maxlength=None,
        value="",
        transform=None,
    ):
        self.tag = tag
        self.text = text
        self.aria_label = aria_label
        self.role = role
        self.input_type = (input_type or "text") if tag == "input" else None
        self.box = box
        self.visible = visible
        self.enabled = enabled
        self.readonly = readonly
        self.content_editable = content_editable
        self.maxlength = maxlength
        self.value = value
        self.transform = transform
        self.connected = True
        self.fills = []
        self.clicks = 0

    def _check_attached(self):
        if not self.connected:
            raise RuntimeError("Element is not attached to the DOM")

    def matches(self, selector: str) -> bool:
        for part in (p.strip() for p in selector.split(",")):
            if part == self.tag:
                return True
            if part == "[aria-label]" and self.aria_label is not None:
                return True
            if part == "[role]" and self.role is not None:
                return True
        return False

    def inner_text(self):
        self._check_attached()
        return self.value if self.content_editable else self.text

    def get_attribute(self, name):
        self._check_attached()
        return {"aria-label": self.aria_label, "role": self.role}.get(name)

    def evaluate(self, expression, *args):
        self._check_attached()
        if expression.strip() == _PROBE_TAG_ONLY:
            return self.tag.upper()
        return {
            "connected": self.connected,
            "tag": self.tag,
            "type": self.input_type or "",
            "contentEditable": self.content_editable,
        }

    def bounding_box(self):
        self._check_attached()
        x, y, w, h = self.box
        return {"x": x, "y": y, "width": w, "height": h}

    def is_visible(self):
        self._check_attached()
        return self.visible

    def is_enabled(self):
        self._check_attached()
        return self.enabled

    def _fillable(self):
        if self.content_editable:
            return True
        if self.tag == "textarea":
            return True
        return self.tag == "input" and self.input_type in _FILLABLE

    def is_editable(self):
        self._check_attached()
        if not self._fillable() and self.tag not in {"select"}:
            raise RuntimeError("Element is not an <input>, <textarea>, <select> or [contenteditable]")
        return self.enabled and not self.readonly

    def click(self, **kwargs):
        self._check_attached()
        self.clicks += 1

    def fill(self, text, **kwargs):
        self._check_attached()
        if not self._fillable():
            raise RuntimeError("Element is not an <input>, <textarea> or [contenteditable] element")
        self.fills.append(text)
        value = text
        if self.maxlength is not None:
            value = value[: self.maxlength]
        if self.transform is not None:
            value = self.transform(value)
        self.value = value

    def input_value(self):
        self._check_attached()
        if self.tag not in {"input", "textarea", "select"}:
            raise RuntimeError("Not an input element")
        return self.value

    def dispose(self):
        pass


class FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text):
        self.typed.append(text)

    def press(self, keys):
        self.pressed.append(keys)


class FakeMouse:
    def __init__(self):
        self.clicks = []
        self.wheels = []

    def click(self, x, y):
        self.clicks.append((x, y))

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class FakePage:
    def __init__(self, elements, title="fixture", url="http://fixture.test/"):
        self.elements = list(elements)
        self._title = title
        self.url = url
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.selector_calls = []
        self.gotos = []
        self.fail_query = False

    def title(self):
        return self._title

    def screenshot(self, **kwargs):
        return b"png"

    def query_selector_all(self, selector):
        self.selector_calls.append(selector)
        if self.fail_query:
            raise RuntimeError("Execution context was destroyed")
        return [el for el in self.elements if el.connected and el.matches(selector)]

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def replace(self, old, new):
        old.connected = False
        self.elements[self.elements.index(old)] = new


def _adapter(page):
    adapter = BrowserAdapter()
    adapter._page = page
    return adapter


def _fixture_page(**input_kwargs):
    heading = FakeHandle("h1", text="Title", box=(8, 8, 1264, 37))
    field = FakeHandle("input", aria_label="query", box=(8, 66, 185, 21), **input_kwargs)
    button = FakeHandle("button", text="Go", box=(193, 66, 33, 21))
    return FakePage([heading, field, button]), heading, field, button


def _observed_id(observation, control_type):
    return next(e.element_id for e in observation.elements if e.control_type == control_type)


# ---- R1 / ACT-01: one observation-scoped map ---------------------------------


def test_type_targets_the_observed_input_not_another_index():
    page, _, field, button = _fixture_page()
    adapter = _adapter(page)
    obs = adapter.observe(include_screenshot=False)
    input_id = _observed_id(obs, "input")
    assert input_id == 1

    note = adapter.act({"action": "type", "element_id": input_id, "text": "hello"})

    assert field.value == "hello"
    assert field.fills == ["hello"]
    assert button.fills == []
    assert page.keyboard.typed == []
    assert "hello" in note


def test_actions_never_recompute_handles_with_a_second_selector_list():
    page, heading, field, button = _fixture_page()
    adapter = _adapter(page)
    obs = adapter.observe(include_screenshot=False)
    calls_after_observe = list(page.selector_calls)

    adapter.act({"action": "type", "element_id": _observed_id(obs, "input"), "text": "x"})
    adapter.act({"action": "click", "element_id": _observed_id(obs, "button")})

    assert page.selector_calls == calls_after_observe
    assert button.clicks == 1
    assert heading.clicks == 0
    assert field.value == "x"


def test_observation_text_format_and_ids_are_unchanged():
    page, *_ = _fixture_page()
    obs = _adapter(page).observe(include_screenshot=False)
    assert obs.tree_text().splitlines() == [
        '[0] h1 "Title" @(8,8,1272,45)',
        '[1] input "query" @(8,66,193,87)',
        '[2] button "Go" @(193,66,226,87)',
    ]


def test_click_uses_the_observed_element():
    page, heading, _, button = _fixture_page()
    adapter = _adapter(page)
    obs = adapter.observe(include_screenshot=False)
    note = adapter.act({"action": "click", "element_id": _observed_id(obs, "button")})
    assert note == "clicked element 2"
    assert button.clicks == 1
    assert heading.clicks == 0


# ---- R2 / ACT-02: pre-dispatch rejection, no focus fallback ------------------


def test_unknown_element_id_is_rejected_without_keyboard_fallback():
    page, *_ = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError, match="not in the current observation"):
        adapter.act({"action": "type", "element_id": 42, "text": "hello"})
    assert page.keyboard.typed == []


def test_element_id_before_any_observation_is_rejected():
    page, _, field, _ = _fixture_page()
    adapter = _adapter(page)
    with pytest.raises(AdapterError, match="not in the current observation"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert field.fills == []
    assert page.keyboard.typed == []


def test_replaced_dom_node_does_not_inherit_old_id():
    page, _, field, _ = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    replacement = FakeHandle("input", aria_label="query", box=(8, 66, 185, 21))
    page.replace(field, replacement)

    with pytest.raises(AdapterError, match="stale"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert replacement.fills == []
    assert field.fills == []
    assert page.keyboard.typed == []

    obs = adapter.observe(include_screenshot=False)
    adapter.act({"action": "type", "element_id": _observed_id(obs, "input"), "text": "hello"})
    assert replacement.value == "hello"


def test_stale_click_is_rejected():
    page, _, _, button = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    button.connected = False
    with pytest.raises(AdapterError, match="stale"):
        adapter.act({"action": "click", "element_id": 2})
    assert button.clicks == 0
    assert page.mouse.clicks == []


def test_navigation_invalidates_the_element_map():
    page, _, field, _ = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    adapter.act({"action": "navigate", "url": "http://fixture.test/other"})
    with pytest.raises(AdapterError, match="not in the current observation"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert field.fills == []


def test_failed_observation_does_not_keep_previous_ids():
    page, _, field, _ = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    page.fail_query = True
    obs = adapter.observe(include_screenshot=False)
    assert obs.elements == []
    with pytest.raises(AdapterError, match="not in the current observation"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert field.fills == []


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"visible": False}, "not visible"),
        ({"enabled": False}, "disabled"),
        ({"readonly": True}, "not editable"),
        ({"input_type": "checkbox"}, "not editable"),
    ],
)
def test_type_into_unusable_input_is_rejected(kwargs, reason):
    page, _, field, _ = _fixture_page(**kwargs)
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError, match=reason):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert field.fills == []
    assert page.keyboard.typed == []


def test_type_into_non_editable_element_is_rejected():
    page, _, _, button = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError, match="not editable"):
        adapter.act({"action": "type", "element_id": 2, "text": "hello"})
    assert button.fills == []
    assert page.keyboard.typed == []


def test_hidden_or_disabled_click_target_is_rejected():
    page, _, _, button = _fixture_page()
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    button.enabled = False
    with pytest.raises(AdapterError, match="disabled"):
        adapter.act({"action": "click", "element_id": 2})
    button.enabled = True
    button.visible = False
    with pytest.raises(AdapterError, match="not visible"):
        adapter.act({"action": "click", "element_id": 2})
    assert button.clicks == 0


def test_rejection_happens_in_prepare_action_before_dispatch():
    page, _, field, _ = _fixture_page()
    inner = _adapter(page)
    guarded = PolicyAdapter(inner)
    guarded.observe(include_screenshot=False)
    field.connected = False

    dispatched = []
    inner.dispatch_prepared_action = lambda action: dispatched.append(action)
    with pytest.raises(AdapterError, match="stale"):
        guarded.prepare_action({"action": "type", "element_id": 1, "text": "hello"})
    with pytest.raises(AdapterError, match="not in the current observation"):
        guarded.prepare_action({"action": "click", "element_id": 9})
    assert dispatched == []


def test_prepare_action_accepts_a_valid_observed_target():
    page, *_ = _fixture_page()
    guarded = PolicyAdapter(_adapter(page))
    guarded.observe(include_screenshot=False)
    prepared = guarded.prepare_action({"action": "type", "element_id": 1, "text": "hello"})
    assert prepared["element_id"] == 1


# ---- R3 / ACT-06: read-back verification -------------------------------------


def test_value_mismatch_after_type_is_a_failure():
    page, _, field, _ = _fixture_page(maxlength=3)
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError, match="did not match"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})
    assert page.keyboard.typed == []


def test_page_rewritten_value_is_a_failure():
    page, *_ = _fixture_page(transform=lambda value: "")
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError, match="did not match"):
        adapter.act({"action": "type", "element_id": 1, "text": "hello"})


def test_password_mismatch_never_reveals_typed_text():
    page, *_ = _fixture_page(input_type="password", maxlength=4)
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError) as excinfo:
        adapter.act({"action": "type", "element_id": 1, "text": "hunter22"})
    message = str(excinfo.value)
    assert "did not match" in message
    assert "hunter" not in message
    assert "hunt" not in message
    chained = excinfo.value.__cause__ or excinfo.value.__context__
    assert chained is None or "hunter" not in str(chained)


def test_password_fill_failure_never_reveals_typed_text():
    page, _, field, _ = _fixture_page(input_type="password")

    def broken_fill(text, **kwargs):
        raise RuntimeError(f"fill({text!r}) failed")

    field.fill = broken_fill
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    with pytest.raises(AdapterError) as excinfo:
        adapter.act({"action": "type", "element_id": 1, "text": "hunter22"})
    assert "hunter" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_password_success_note_does_not_record_value():
    page, _, field, _ = _fixture_page(input_type="password")
    adapter = _adapter(page)
    adapter.observe(include_screenshot=False)
    note = adapter.act({"action": "type", "element_id": 1, "text": "hunter22"})
    assert field.value == "hunter22"
    assert "hunter" not in note


def test_contenteditable_is_verified_by_text():
    editor = FakeHandle("div", role="textbox", aria_label="notes", content_editable=True)
    page = FakePage([editor])
    adapter = _adapter(page)
    obs = adapter.observe(include_screenshot=False)
    adapter.act({"action": "type", "element_id": obs.elements[0].element_id, "text": "a note"})
    assert editor.value == "a note"

    editor.transform = lambda value: value.upper()
    with pytest.raises(AdapterError, match="did not match"):
        adapter.act({"action": "type", "element_id": 0, "text": "a note"})


# ---- R5: other behaviour unchanged -------------------------------------------


def test_untargeted_type_key_scroll_and_coordinate_click_are_unchanged():
    page, *_ = _fixture_page()
    adapter = _adapter(page)
    assert adapter.act({"action": "type", "text": "free"}) == "typed 'free'"
    assert page.keyboard.typed == ["free"]
    assert adapter.act({"action": "key", "keys": "ctrl+s"}) == "pressed ctrl+s"
    assert page.keyboard.pressed == ["Control+s"]
    assert adapter.act({"action": "scroll", "direction": "down", "amount": 2}) == "scrolled down"
    assert page.mouse.wheels == [(0, 200)]
    assert adapter.act({"action": "click", "x": 5, "y": 6}) == "clicked (5,6)"
    assert page.mouse.clicks == [(5.0, 6.0)]


# ---- real Chromium fixture layer ---------------------------------------------

_LAUNCH_SKIP = 77

_HARNESS = r'''
import glob, json, os, sys
from playwright.sync_api import BrowserType
from argus.adapters.base import AdapterError, create_adapter

_orig_launch = BrowserType.launch


def _local_chromium():
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium_headless_shell-*/chrome-*/*shell"):
        hits = sorted(glob.glob(os.path.join(root, pattern)))
        if hits:
            return hits[-1]
    return None


def _launch(self, *args, **kwargs):
    try:
        return _orig_launch(self, *args, **kwargs)
    except Exception:
        exe = _local_chromium()
        if not exe or "executable_path" in kwargs:
            raise
        return _orig_launch(self, *args, executable_path=exe, **kwargs)


BrowserType.launch = _launch
adapter = create_adapter("browser")
try:
    adapter.launch("about:blank")
except Exception as exc:
    print("LAUNCH-FAILED", type(exc).__name__, file=sys.stderr)
    try:
        adapter.close()
    except Exception:
        pass
    sys.exit(77)
page = adapter.inner._page
out = {}


def attempt(action):
    try:
        return {"ok": True, "note": adapter.act(action)}
    except AdapterError as exc:
        return {"ok": False, "error": str(exc)}


def ids(obs):
    return {e.control_type + ":" + e.name: e.element_id for e in obs.elements}


try:
{body}
finally:
    adapter.close()
print("RESULT" + json.dumps(out))
'''


def _run_browser(body: str) -> dict:
    script = _HARNESS.replace("{body}", textwrap.indent(textwrap.dedent(body), "    "))
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        pytest.skip("real browser fixture timed out")
    if proc.returncode == _LAUNCH_SKIP or "No module named 'playwright'" in proc.stderr:
        pytest.skip("no Playwright browser can launch here")
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT"))
    return json.loads(line[len("RESULT"):])


_BASIC_FIXTURE = (
    "<title>fx</title><h1>Title</h1>"
    "<input id='q' aria-label='query'><button id='go'>Go</button>"
)


def test_real_browser_type_lands_in_observed_input():
    out = _run_browser(
        f"""
        page.set_content({_BASIC_FIXTURE!r})
        obs = adapter.observe(include_screenshot=False)
        out["ids"] = ids(obs)
        out["type"] = attempt({{"action": "type", "element_id": ids(obs)["input:query"],
                               "text": "hello"}})
        out["value"] = page.input_value("#q")
        out["click"] = attempt({{"action": "click", "element_id": ids(obs)["button:Go"]}})
        """
    )
    assert out["ids"]["input:query"] == 1
    assert out["type"]["ok"], out["type"]
    assert out["value"] == "hello"
    assert out["click"] == {"ok": True, "note": "clicked element 2"}


def test_real_browser_replaced_input_is_stale_until_reobserved():
    out = _run_browser(
        f"""
        page.set_content({_BASIC_FIXTURE!r})
        obs = adapter.observe(include_screenshot=False)
        old_id = ids(obs)["input:query"]
        page.evaluate("() => document.getElementById('q').replaceWith("
                      "Object.assign(document.createElement('input'), "
                      "{{id: 'q', ariaLabel: 'query'}}))")
        page.focus("#q")
        out["stale"] = attempt({{"action": "type", "element_id": old_id, "text": "hello"}})
        out["after_stale"] = page.input_value("#q")
        obs = adapter.observe(include_screenshot=False)
        out["retry"] = attempt({{"action": "type", "element_id": ids(obs)["input:query"],
                                "text": "hello"}})
        out["after_retry"] = page.input_value("#q")
        page.set_content("<title>next</title><p>other page</p>")
        out["navigated"] = attempt({{"action": "click", "element_id": old_id}})
        """
    )
    assert not out["stale"]["ok"]
    assert "stale" in out["stale"]["error"]
    assert out["after_stale"] == ""
    assert out["retry"]["ok"], out["retry"]
    assert out["after_retry"] == "hello"
    assert not out["navigated"]["ok"]


def test_real_browser_rejects_unusable_targets_before_typing():
    fixture = (
        "<title>fx</title>"
        "<input id='hidden' aria-label='hidden' style='display:none'>"
        "<input id='off' aria-label='off' disabled>"
        "<input id='ro' aria-label='ro' readonly>"
        "<input id='box' aria-label='box' type='checkbox'>"
        "<button id='go'>Go</button>"
        "<input id='focus' aria-label='focus' autofocus>"
    )
    out = _run_browser(
        f"""
        page.set_content({fixture!r})
        page.focus("#focus")
        obs = adapter.observe(include_screenshot=False)
        found = ids(obs)
        for key in ("input:hidden", "input:off", "input:ro", "input:box", "button:Go"):
            out[key] = attempt({{"action": "type", "element_id": found[key], "text": "zz"}})
        out["missing"] = attempt({{"action": "type", "element_id": 999, "text": "zz"}})
        out["focus_value"] = page.input_value("#focus")
        out["values"] = page.evaluate(
            "() => ['off', 'ro'].map(id => document.getElementById(id).value)")
        """
    )
    assert not out["input:hidden"]["ok"] and "not visible" in out["input:hidden"]["error"]
    assert not out["input:off"]["ok"] and "disabled" in out["input:off"]["error"]
    assert not out["input:ro"]["ok"] and "not editable" in out["input:ro"]["error"]
    assert not out["input:box"]["ok"] and "not editable" in out["input:box"]["error"]
    assert not out["button:Go"]["ok"] and "not editable" in out["button:Go"]["error"]
    assert not out["missing"]["ok"]
    assert "not in the current observation" in out["missing"]["error"]
    assert out["focus_value"] == ""
    assert out["values"] == ["", ""]


def test_real_browser_read_back_mismatch_fails_and_hides_passwords():
    fixture = (
        "<title>fx</title>"
        "<input id='short' aria-label='short' maxlength='3'>"
        "<input id='pw' aria-label='pw' type='password' maxlength='4'>"
        "<input id='pw2' aria-label='pw2' type='password'>"
        "<div id='ed' aria-label='editor' contenteditable='true'></div>"
    )
    out = _run_browser(
        f"""
        page.set_content({fixture!r})
        obs = adapter.observe(include_screenshot=False)
        found = ids(obs)
        out["short"] = attempt({{"action": "type", "element_id": found["input:short"],
                                "text": "hello"}})
        out["pw"] = attempt({{"action": "type", "element_id": found["input:pw"],
                             "text": "hunter22"}})
        out["pw2"] = attempt({{"action": "type", "element_id": found["input:pw2"],
                              "text": "hunter22"}})
        out["pw2_value"] = page.input_value("#pw2")
        out["ed"] = attempt({{"action": "type", "element_id": found["div:editor"],
                             "text": "a note"}})
        out["ed_text"] = page.inner_text("#ed")
        """
    )
    assert not out["short"]["ok"] and "did not match" in out["short"]["error"]
    assert not out["pw"]["ok"] and "did not match" in out["pw"]["error"]
    assert "hunt" not in out["pw"]["error"]
    assert out["pw2"]["ok"] and "hunter" not in out["pw2"]["note"]
    assert out["pw2_value"] == "hunter22"
    assert out["ed"]["ok"], out["ed"]
    assert out["ed_text"] == "a note"
