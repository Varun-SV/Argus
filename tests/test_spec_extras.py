"""Tests for new spec features: retries, new assertion kinds, UTF-8 fallback."""
import pytest

from argus.engine.spec import parse_spec, SpecError, ASSERTION_KINDS


def test_retries_defaults_to_zero():
    spec = parse_spec(
        "target: {adapter: cli, launch: echo hi}\nsteps:\n  - 'do thing'\n"
    )
    assert spec.retries == 0


def test_retries_parsed():
    spec = parse_spec(
        "target: {adapter: cli, launch: echo hi}\nretries: 3\nsteps:\n  - 'do thing'\n"
    )
    assert spec.retries == 3


def test_stdout_contains_assertion():
    spec = parse_spec(
        "target: {adapter: cli, launch: echo hi}\n"
        "steps:\n  - assert:\n      stdout_contains: 'hi'\n"
    )
    assert spec.steps[0].assertion == "stdout_contains"


def test_stderr_contains_assertion():
    assert "stderr_contains" in ASSERTION_KINDS


def test_exit_code_is_assertion():
    spec = parse_spec(
        "target: {adapter: cli, launch: echo hi}\n"
        "steps:\n  - assert:\n      exit_code_is: 0\n"
    )
    assert spec.steps[0].assertion == "exit_code_is"
    assert spec.steps[0].expected == 0


def test_url_contains_assertion():
    assert "url_contains" in ASSERTION_KINDS


def test_page_title_contains_assertion():
    spec = parse_spec(
        "target: {adapter: browser, launch: 'http://example.com'}\n"
        "steps:\n  - assert:\n      page_title_contains: Example\n"
    )
    assert spec.steps[0].assertion == "page_title_contains"


def test_unknown_assertion_still_raises():
    with pytest.raises(SpecError, match="unknown assertion"):
        parse_spec(
            "target: {adapter: cli, launch: echo}\n"
            "steps:\n  - assert:\n      pixel_color_is: '#fff'\n"
        )
