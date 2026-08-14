"""Privacy-aware runtime projection for canonical ATES evidence.

This layer builds on the structural runtime recorder introduced earlier.  The
execution objects themselves are never redacted or rewritten; only the
separate evidence projection is classified by :mod:`argus.ates.privacy` before
it reaches the event store.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from argus.adapters.base import Adapter, Observation
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
    EvidenceContext,
    EvidencePrivacyPolicy,
    EvidenceValue,
    ExecutionKind,
    FindingId,
    FindingRecord,
    ObservationId,
    ObservationRecord,
    RoamSource,
    RunId,
    RunRecord,
    ScriptedSource,
    StepId,
    StepRecord,
    to_json_compatible,
)
from argus.engine import ates_runtime as _base
from argus.engine.spec import AssertStep, TestSpec

AtesRuntimeError = _base.AtesRuntimeError
ActionOutcomeUnresolvedError = _base.ActionOutcomeUnresolvedError
resolve_runtime_project_dir = _base.resolve_runtime_project_dir


class AtesRuntimeRecorder(_base.AtesRuntimeRecorder):
    """Runtime recorder whose free-form fields pass through privacy policy."""

    def __init__(
        self,
        project_dir,
        run_record: RunRecord,
        steps: Sequence[StepRecord],
        environment: Mapping[str, object],
        *,
        roam_step: Optional[StepRecord] = None,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
        target_value: Optional[str] = None,
    ) -> None:
        self.privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        self._privacy_target_value = target_value
        super().__init__(
            project_dir,
            run_record,
            steps,
            environment,
            roam_step=roam_step,
        )

    @classmethod
    def for_scripted(
        cls,
        project_dir,
        spec: TestSpec,
        provider,
        adapter: Adapter,
        *,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
    ) -> "AtesRuntimeRecorder":
        privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        identity_key = _base._identity_key(project_dir)
        source_commitment = _base._protected_commitment(
            identity_key,
            _base._scripted_source_shape(spec),
            profile="ates-runtime-scripted-source-v1",
        )
        test_case_id = "TEST-" + _base._opaque_identity(
            identity_key,
            "test-case",
            _base._scripted_test_identity_shape(project_dir, spec),
        )
        source = ScriptedSource(
            test_case_id=test_case_id,
            commitment=source_commitment,
        )
        environment = _base._environment_shape(adapter)
        model_identity = _base._model_identity(identity_key, provider)
        config = _base._protected_commitment(
            identity_key,
            _base._configuration_shape(
                execution_kind=ExecutionKind.SCRIPTED,
                provider=provider,
                adapter=adapter,
                model_identity=model_identity,
                extra={
                    "continue_on_failure": bool(spec.continue_on_failure),
                    "retries": int(spec.retries),
                    "staging_count": len(spec.staging),
                    "collect_count": len(spec.collect),
                    "privacy_policy": privacy.policy_id,
                },
            ),
            profile="ates-runtime-config-v1",
        )
        run_record = RunRecord(
            run_id=RunId.new(),
            execution_kind=ExecutionKind.SCRIPTED,
            source=source,
            started_at=_base._utc_now(),
            argus_version=_base.__version__,
            adapter_type=_base._adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=privacy.policy_id,
            configuration_commitment=config,
            provider=_base._provider_type(provider),
            model_provider=_base._provider_type(provider),
            model=model_identity,
        )
        steps = tuple(
            StepRecord(
                step_id=StepId.new(),
                instruction=privacy.capture(
                    step.describe() if isinstance(step, AssertStep) else step.text,
                    context=EvidenceContext.STEP_INSTRUCTION,
                    field_name="instruction",
                ),
                kind=str(step.kind),
            )
            for step in spec.steps
        )
        return cls(
            project_dir,
            run_record,
            steps,
            environment,
            privacy_policy=privacy,
            target_value=spec.launch,
        )

    @classmethod
    def for_roam(
        cls,
        project_dir,
        provider,
        adapter: Adapter,
        *,
        target: str,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
    ) -> "AtesRuntimeRecorder":
        privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        identity_key = _base._identity_key(project_dir)
        environment = _base._environment_shape(adapter)
        model_identity = _base._model_identity(identity_key, provider)
        target_identity = "TARGET-" + _base._opaque_identity(
            identity_key,
            "roam-target",
            target,
        )
        source_config = _base._protected_commitment(
            identity_key,
            {
                "kind": "roam_session",
                "target": target,
                "provider_type": _base._provider_type(provider),
                "provider_model": _base._provider_model(provider),
                "adapter_type": _base._adapter_type(adapter),
                "privacy_policy": privacy.policy_id,
            },
            profile="ates-runtime-roam-source-v1",
        )
        source = RoamSource(
            objective_present=False,
            config_commitment=source_config,
        )
        config = _base._protected_commitment(
            identity_key,
            _base._configuration_shape(
                execution_kind=ExecutionKind.ROAM,
                provider=provider,
                adapter=adapter,
                model_identity=model_identity,
                extra={
                    "target_identity": target_identity,
                    "privacy_policy": privacy.policy_id,
                },
            ),
            profile="ates-runtime-config-v1",
        )
        run_record = RunRecord(
            run_id=RunId.new(),
            execution_kind=ExecutionKind.ROAM,
            source=source,
            started_at=_base._utc_now(),
            argus_version=_base.__version__,
            adapter_type=_base._adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=privacy.policy_id,
            configuration_commitment=config,
            provider=_base._provider_type(provider),
            model_provider=_base._provider_type(provider),
            model=model_identity,
        )
        roam_step = StepRecord(
            step_id=StepId.new(),
            instruction=privacy.capture(
                target,
                context=EvidenceContext.TARGET,
                field_name="target",
            ),
            kind="roam",
        )
        return cls(
            project_dir,
            run_record,
            (roam_step,),
            environment,
            roam_step=roam_step,
            privacy_policy=privacy,
            target_value=target,
        )

    def target_launched(self) -> None:
        self._append(
            _base.EventType.TARGET_LAUNCHED,
            {
                "target": to_json_compatible(
                    self.privacy.capture(
                        self._privacy_target_value,
                        context=EvidenceContext.TARGET,
                        field_name="target",
                    )
                )
            },
        )

    def record_observation(self, obs: Observation, source: str) -> ObservationId:
        current = self._current
        if current is None:
            raise AtesRuntimeError("observation occurred outside an active step attempt")
        facts = {
            "process_alive": EvidenceValue.safe(bool(obs.process_alive)),
            "element_count": EvidenceValue.safe(len(obs.elements)),
            "dialog_count": EvidenceValue.safe(len(obs.dialogs)),
            "has_error": EvidenceValue.safe(bool(obs.error)),
            "has_stdout": EvidenceValue.safe(obs.stdout is not None),
            "has_stderr": EvidenceValue.safe(obs.stderr is not None),
            "has_url": EvidenceValue.safe(obs.url is not None),
            "screenshot_present": EvidenceValue.safe(obs.screenshot_png is not None),
            "window_title": self.privacy.observation_value("window_title", obs.window_title),
            "ui_tree": self.privacy.observation_value("ui_tree", obs.tree_text()),
            "dialogs": self.privacy.observation_value("dialogs", list(obs.dialogs)),
            "error": self.privacy.observation_value("error", obs.error),
            "stdout": self.privacy.observation_value("stdout", obs.stdout),
            "stderr": self.privacy.observation_value("stderr", obs.stderr),
            "url": self.privacy.observation_value("url", obs.url),
        }
        record = ObservationRecord(
            observation_id=ObservationId.new(),
            step_attempt_id=current.attempt_id,
            source=source,
            captured_at=_base._utc_now(),
            capture_policy=self.privacy.policy_id,
            facts=facts,
        )
        self._append(
            _base.EventType.OBSERVATION_CAPTURED,
            {"observation": to_json_compatible(record)},
        )
        self._latest_observation_id = record.observation_id
        return record.observation_id

    def _project_action_record(
        self,
        action: ActionRecord,
        values: Mapping[str, object],
        *,
        validated: bool,
    ) -> ActionRecord:
        kind, parameter_keys = _base._safe_action_structure(values)
        if validated and kind == "invalid":
            raise AtesRuntimeError("validated action cannot have an invalid structural kind")
        parameters = {
            key: self.privacy.action_parameter(
                kind,
                key,
                values.get(key),
                validated=validated,
            )
            for key in parameter_keys
        }
        return ActionRecord(
            action_id=action.action_id,
            step_id=action.step_id,
            step_attempt_id=action.step_attempt_id,
            action_type=kind,
            parameters=parameters,
            operation_id=action.operation_id,
        )

    def record_action_proposed(self, action: Mapping[str, object]) -> ActionRecord:
        current = self._current
        if current is None:
            raise AtesRuntimeError("action occurred outside an active step attempt")
        seed = ActionRecord(
            action_id=ActionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            action_type=_base._safe_action_structure(action)[0],
            parameters={},
            operation_id=ActionOperationId.new(),
        )
        record = self._project_action_record(seed, action, validated=False)
        self._append(
            _base.EventType.ACTION_PROPOSED,
            {"action": to_json_compatible(record)},
        )
        return record

    def record_action_policy_validated(
        self,
        action: ActionRecord,
        normalized: Mapping[str, object],
    ) -> ActionRecord:
        validated = self._project_action_record(action, normalized, validated=True)
        self._append(
            _base.EventType.ACTION_POLICY_VALIDATED,
            {"action": to_json_compatible(validated)},
        )
        return validated

    def record_action_dispatch_committed(
        self,
        action: ActionRecord,
        normalized: Mapping[str, object],
    ) -> ActionRecord:
        committed = self._project_action_record(action, normalized, validated=True)
        self._append(
            _base.EventType.ACTION_DISPATCH_COMMITTED,
            {"action": to_json_compatible(committed)},
        )
        return committed

    def record_action_outcome_unknown(
        self,
        action: ActionRecord,
        error: object = None,
    ) -> None:
        self._append(
            _base.EventType.ACTION_OUTCOME_UNKNOWN,
            {
                "action_id": str(action.action_id),
                "operation_id": str(action.operation_id) if action.operation_id else None,
                "error": to_json_compatible(self.privacy.error_text(error)),
            },
        )

    def record_assertion(
        self,
        step: AssertStep,
        status: str,
        actual_present: bool,
        actual_value: object = None,
    ) -> AssertionId:
        current = self._current
        if current is None:
            raise AtesRuntimeError("assertion occurred outside an active step attempt")
        actual = None
        if actual_present:
            # Older callers may know only that an actual value existed.  Feed a
            # non-persisting sentinel through the same target-generated policy
            # rather than accidentally claiming the value was absent.
            actual = self.privacy.assertion_actual(
                actual_value if actual_value is not None else "<present>"
            )
        record = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            kind=step.assertion,
            expected=self.privacy.assertion_expected(step.expected),
            result=_base._assertion_result(status),
            method="deterministic.adapter_observation",
            observation_id=self._latest_observation_id,
            actual=actual,
            required=True,
        )
        self._append(
            _base.EventType.ASSERTION_EVALUATED,
            {"assertion": to_json_compatible(record)},
        )
        return record.assertion_id

    def record_finding(
        self,
        *,
        source: str,
        classification: str,
        title: object = None,
        description: object = None,
    ) -> FindingId:
        refs: tuple[str, ...] = ()
        if self._latest_observation_id is not None:
            refs = (str(self._latest_observation_id),)
        record = FindingRecord(
            finding_id=FindingId.new(),
            title=self.privacy.finding_title(title),
            description=self.privacy.finding_description(description),
            evidence_refs=refs,
            classification_source=_base._safe_finding_source(source),
            classification=_base._safe_finding_classification(classification),
        )
        self._append(
            _base.EventType.FINDING_RECORDED,
            {"finding": to_json_compatible(record)},
        )
        return record.finding_id


class AtesAdapterProxy(_base.AtesAdapterProxy):
    """Dispatch proxy that also routes runtime error text through privacy policy."""

    def act(self, action: dict) -> str:
        self._ensure_no_unresolved_dispatch()
        proposed = self.recorder.record_action_proposed(action)
        normalized = self.inner.prepare_action(action)
        validated = self.recorder.record_action_policy_validated(proposed, normalized)
        committed = self.recorder.record_action_dispatch_committed(validated, normalized)

        try:
            note = self.inner.dispatch_prepared_action(normalized)
        except BaseException as exc:
            self._unresolved_action = committed
            if not self.recorder.failed:
                try:
                    self.recorder.record_action_outcome_unknown(committed, error=exc)
                except BaseException as evidence_exc:
                    raise evidence_exc from exc
            raise

        try:
            self.recorder.record_action_executed(committed)
        except BaseException:
            self._unresolved_action = committed
            raise
        return note


__all__ = [
    "ActionOutcomeUnresolvedError",
    "AtesAdapterProxy",
    "AtesRuntimeError",
    "AtesRuntimeRecorder",
    "resolve_runtime_project_dir",
]
