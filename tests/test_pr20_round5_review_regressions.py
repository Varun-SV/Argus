from types import SimpleNamespace

import pytest

from argus.adapters.base import Observation
from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
from argus.engine.ates_runtime import AtesRuntimeError, AtesRuntimeRecorder
from argus.engine.spec import AssertStep, NLStep, SpecError, TestSpec
from tests.conftest import FakeAdapter, FakeProvider


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(str(run_id))) as store:
        return tuple(store.events)


def _valid_spec(step):
    return TestSpec(
        name="programmatic privacy regression",
        adapter="desktop-gui",
        launch="fake.exe",
        steps=[step],
    )


def test_programmatic_step_and_assertion_metadata_use_trusted_vocab():
    with pytest.raises(SpecError, match="unknown natural-language step kind"):
        NLStep(text="safe", kind="customer-secret")

    with pytest.raises(SpecError, match="unknown assertion"):
        AssertStep(assertion="customer-secret", expected=True)

    with pytest.raises(SpecError, match="assertion step kind"):
        AssertStep(assertion="process_running", expected=True, kind="customer-secret")


def test_runtime_rechecks_mutated_programmatic_step_metadata(tmp_path):
    step = NLStep(text="safe")
    spec = _valid_spec(step)
    step.kind = "customer-secret"

    with pytest.raises(AtesRuntimeError, match="trusted vocabulary"):
        AtesRuntimeRecorder.for_scripted(
            tmp_path,
            spec,
            FakeProvider([]),
            FakeAdapter(),
        )

    assertion = AssertStep(assertion="process_running", expected=True)
    assertion_spec = _valid_spec(assertion)
    assertion.assertion = "customer-secret"

    with pytest.raises(AtesRuntimeError, match="trusted vocabulary"):
        AtesRuntimeRecorder.for_scripted(
            tmp_path,
            assertion_spec,
            FakeProvider([]),
            FakeAdapter(),
        )


class _UnsafeProvider(FakeProvider):
    type_name = "token=private-provider"


class _UnsafeAdapter(FakeAdapter):
    type_name = "url=https://private-adapter"

    def info(self):
        return SimpleNamespace(
            environment_type="credential=private-environment",
            isolated=False,
        )


def test_extension_structural_labels_cannot_reach_canonical_jsonl(tmp_path):
    provider = _UnsafeProvider([])
    adapter = _UnsafeAdapter()
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _valid_spec(NLStep(text="safe")),
        provider,
        adapter,
    )
    run_id = recorder.run_id

    assert recorder.run_record.provider == "custom-provider"
    assert recorder.run_record.model_provider == "custom-provider"
    assert recorder.run_record.adapter_type == "custom-adapter"
    assert recorder.run_record.environment_type == "custom-environment"

    recorder.begin_step(0, 1)
    recorder.record_observation(
        Observation(window_title="safe"),
        "source=private-observation-source",
    )
    recorder.complete_current("pass")
    recorder.close()

    events = _events(tmp_path, run_id)
    observation = next(
        to_json_compatible(event.payload)["observation"]
        for event in events
        if event.envelope.event_type is EventType.OBSERVATION_CAPTURED
    )
    assert observation["source"] == "custom-adapter"

    persisted = b"".join(event.canonical_line() for event in events)
    for secret in (
        b"token=private-provider",
        b"url=https://private-adapter",
        b"credential=private-environment",
        b"source=private-observation-source",
    ):
        assert secret not in persisted
