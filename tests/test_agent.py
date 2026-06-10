import pytest

from argus.engine.agent import AgentParseError, extract_action


def test_plain_json():
    action = extract_action('{"action": "click", "element_id": 3}')
    assert action == {"action": "click", "element_id": 3}


def test_fenced_json():
    reply = 'Sure!\n```json\n{"action": "type", "text": "hi"}\n```\nDone.'
    assert extract_action(reply)["action"] == "type"


def test_json_with_prose():
    reply = 'I will click the button. {"action": "click", "x": 10, "y": 20} hope that works'
    action = extract_action(reply)
    assert action["x"] == 10


def test_nested_json():
    reply = '{"action": "report_bug", "title": "x", "detail": {"a": 1}}'
    assert extract_action(reply)["title"] == "x"


def test_no_json():
    with pytest.raises(AgentParseError):
        extract_action("I think we should click the save button.")


def test_missing_action_field():
    with pytest.raises(AgentParseError):
        extract_action('{"element_id": 4}')


def test_invalid_json():
    with pytest.raises(AgentParseError):
        extract_action('{"action": "click", broken}')
