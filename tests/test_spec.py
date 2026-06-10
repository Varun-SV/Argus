import pytest

from argus.engine.spec import AssertStep, NLStep, SpecError, parse_spec

GOOD = """\
name: Notepad test
target:
  adapter: desktop-gui
  launch: notepad.exe
setup:
  - "Wait for the window"
steps:
  - "Type hello"
  - assert:
      text_visible: "hello"
  - assert:
      element_exists:
        name: Save
        control_type: MenuItem
teardown:
  - close
"""


def test_parse_good_spec():
    spec = parse_spec(GOOD)
    assert spec.name == "Notepad test"
    assert spec.adapter == "desktop-gui"
    assert spec.launch == "notepad.exe"
    assert len(spec.steps) == 5
    assert isinstance(spec.steps[0], NLStep) and spec.steps[0].kind == "setup"
    assert isinstance(spec.steps[1], NLStep) and spec.steps[1].kind == "step"
    assert isinstance(spec.steps[2], AssertStep)
    assert spec.steps[2].assertion == "text_visible"
    assert isinstance(spec.steps[3], AssertStep)
    assert spec.steps[3].expected == {"name": "Save", "control_type": "MenuItem"}
    assert spec.steps[4].kind == "teardown"


def test_missing_target():
    with pytest.raises(SpecError, match="target"):
        parse_spec("name: x\nsteps:\n  - 'do thing'\n")


def test_no_steps():
    with pytest.raises(SpecError, match="no steps"):
        parse_spec("name: x\ntarget: {adapter: desktop-gui, launch: a.exe}\n")


def test_unknown_assertion():
    bad = """\
target: {adapter: desktop-gui, launch: a.exe}
steps:
  - assert:
      pixel_color_is: "#fff"
"""
    with pytest.raises(SpecError, match="unknown assertion"):
        parse_spec(bad)


def test_multiple_keys_in_assert():
    bad = """\
target: {adapter: desktop-gui, launch: a.exe}
steps:
  - assert:
      text_visible: a
      window_title_contains: b
"""
    with pytest.raises(SpecError, match="exactly one"):
        parse_spec(bad)


def test_invalid_yaml():
    with pytest.raises(SpecError, match="invalid YAML"):
        parse_spec("a: [unclosed")
