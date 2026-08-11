"""Runtime glue between Argus execution engines and canonical ATES evidence.

PR #18 intentionally records only structural, secret-safe runtime evidence.
Free-form step text, target-generated text, action values, assertion values,
model findings, and screenshots remain suppressed until the dedicated privacy
and artifact PRs provide policy-aware capture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from argus import __version__
from argus.adapters.base import Adapter, Observation
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
    AssertionResult,
    AtesEventStore,
    EventType,
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
    SourceCommitment,
    StepAttemptId,
    StepAttemptRecord,
    StepAttemptStatus,
    StepId,
    StepRecord,
    to_json_compatible,
)
from argus.engine.spec import AssertStep, TestSpec

_RUNTIME_PROFILE = "runtime-structural-v1"
_PRIVACY_REASON = "runtime.privacy_pending"
_TARGET_REASON = "runtime.target_value"
_ACTION_REASON = "runtime.action_value"
_ASSERTION_REASON = "runtime.assertion_value"
_FINDING_REASON = "runtime.finding_text"
_RETRY_REASON = "runtime.retry_reason"


class AtesRuntimeError(RuntimeError):
    """Raised when mandatory runtime evidence cannot be represented safely."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _commitment(value: object) -> SourceCommitment:
    """Create a secret-safe commitment over an already-redacted structure."""
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return SourceCommitment(
        method="sha256_redacted_canonical",
        value="sha256:" + hashlib.sha256(encoded).hexdigest(),
        canonicalization_profile="ates-runtime-redacted-v1",
    )


def _provider_type(provider) -> str:
    value = getattr(provider, "type_name", None) or type(provider).__name__
    return str(value).strip() or "unknown-provider"


def _adapter_type(adapter: Adapter) -> str:
    value = getattr(adapter, "type_name", None) or type(adapter).__name__
    return str(value).strip() or "unknown-adapter"


def _environment_shape(adapter: Adapter) -> dict[str, object]:
    try:
        info = adapter.info()
    except Exception:
        return {
            "environment_type": "direct",
            "isolated": False,
        }
    return {
        "environment_type": str(getattr(info, "environment_type", "direct")),
        "isolated": bool(getattr(info, "isolated", False)),
    }


def resolve_runtime_project_dir(
    project_dir: Optional[Path],
    *,
    spec_path: Optional[Path] = None,
    session_dir: Optional[Path] = None,
) -> Path:
    """Resolve the project trust root used by the ATES event store.

    Normal CLI/API calls should pass the Argus project root. For backwards-
    compatible direct ``run_test`` calls without a project path we use the
    current working directory. Direct roam calls infer the project from a
    ``.argus`` ancestor when present, otherwise from the session directory's
    parent.
    """
    if project_dir is not None:
        return Path(project_dir).resolve(strict=True)

    if spec_path is not None:
        path = Path(spec_path)
        if path.parent.name == ".argus":
            return path.parent.parent.resolve(strict=True)

    if session_dir is not None:
        path = Path(session_dir)
        candidates = (path, *path.parents)
        for candidate in candidates:
            if candidate.name == ".argus":
                return candidate.parent.resolve(strict=True)
        return path.parent.resolve(strict=True)

    return Path.cwd().resolve(strict=True)


def _scripted_source_shape(spec: TestSpec) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for step in spec.steps:
        if isinstance(step, AssertStep):
            steps.append(
                {
                    "kind": step.kind,
                    "type": "assertion",
                    "assertion": step.assertion,
                }
            )
        else:
            steps.append({"kind": step.kind, "type": "natural_language"})
    return {
        "kind": "test_spec",
        "adapter": spec.adapter,
        "steps": steps,
        "continue_on_failure": bool(spec.continue_on_failure),
        "retries": int(spec.retries),
        "staging_count": len(spec.staging),
        "collect_count": len(spec.collect),
        "authored_values": "<redacted>",
    }


def _configuration_shape(
    *,
    execution_kind: ExecutionKind,
    provider,
    adapter: Adapter,
    extra: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    environment = _environment_shape(adapter)
    value: dict[str, object] = {
        "execution_kind": execution_kind.value,
        "provider_type": _provider_type(provider),
        "adapter_type": _adapter_type(adapter),
        **environment,
    }
    if extra:
        value.update(dict(extra))
    return value


def _step_status(value: str) -> StepAttemptStatus:
    mapping = {
        "pass": StepAttemptStatus.PASSED,
        "fail": StepAttemptStatus.FAILED,
        "error": StepAttemptStatus.ERROR,
        "cancelled": StepAttemptStatus.CANCELLED,
        "outcome_unknown": StepAttemptStatus.OUTCOME_UNKNOWN,
    }
    return mapping.get(str(value).lower(), StepAttemptStatus.ERROR)


def _assertion_result(value: str) -> AssertionResult:
    mapping = {
        "pass": AssertionResult.PASSED,
        "fail": AssertionResult.FAILED,
        "error": AssertionResult.ERROR,
        "skipped": AssertionResult.SKIPPED,
    }
    return mapping.get(str(value).lower(), AssertionResult.UNEVALUATED)


@dataclass(frozen=True)
class _AttemptContext:
    step: StepRecord
    attempt_id: StepAttemptId
    ordinal: int
    started_at: datetime
    retry_reason: Optional[EvidenceValue]


class AtesRuntimeRecorder:
    """Durably emits secret-safe structural ATES runtime lifecycle events."""

    def __init__(
        self,
        project_dir: Path,
        run_record: RunRecord,
        steps: Sequence[StepRecord],
        environment: Mapping[str, object],
        *,
        roam_step: Optional[StepRecord] = None,
    ) -> None:
        self.run_record = run_record
        self.run_id = run_record.run_id
        self.steps = tuple(steps)
        self._roam_step = roam_step
        self._current: Optional[_AttemptContext] = None
        self._latest_observation_id: Optional[ObservationId] = None
        self._failed = False
        self._failure: Optional[BaseException] = None
        self._store = AtesEventStore(project_dir, self.run_id)
        self._append(
            EventType.RUN_STARTED,
            {
                "run": to_json_compatible(run_record),
                "steps": [to_json_compatible(step) for step in self.steps],
            },
        )
        self._append(
            EventType.ENVIRONMENT_PREPARED,
            {
                "environment_type": str(environment.get("environment_type", "direct")),
                "isolated": bool(environment.get("isolated", False)),
            },
        )

    @classmethod
    def for_scripted(
        cls,
        project_dir: Path,
        spec: TestSpec,
        provider,
        adapter: Adapter,
    ) -> "AtesRuntimeRecorder":
        source_shape = _scripted_source_shape(spec)
        source_commitment = _commitment(source_shape)
        test_case_id = "TEST-" + source_commitment.value.split(":", 1)[1][:20]
        source = ScriptedSource(
            test_case_id=test_case_id,
            commitment=source_commitment,
        )
        environment = _environment_shape(adapter)
        config = _commitment(
            _configuration_shape(
                execution_kind=ExecutionKind.SCRIPTED,
                provider=provider,
                adapter=adapter,
                extra={
                    "continue_on_failure": bool(spec.continue_on_failure),
                    "retries": int(spec.retries),
                    "staging_count": len(spec.staging),
                    "collect_count": len(spec.collect),
                },
            )
        )
        run_record = RunRecord(
            run_id=RunId.new(),
            execution_kind=ExecutionKind.SCRIPTED,
            source=source,
            started_at=_utc_now(),
            argus_version=__version__,
            adapter_type=_adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=_RUNTIME_PROFILE,
            configuration_commitment=config,
            provider=_provider_type(provider),
        )
        steps = tuple(
            StepRecord(
                step_id=StepId.new(),
                instruction=EvidenceValue.suppressed(_PRIVACY_REASON),
                kind=str(step.kind),
            )
            for step in spec.steps
        )
        return cls(project_dir, run_record, steps, environment)

    @classmethod
    def for_roam(
        cls,
        project_dir: Path,
        provider,
        adapter: Adapter,
    ) -> "AtesRuntimeRecorder":
        environment = _environment_shape(adapter)
        objective_commitment = _commitment(
            {
                "kind": "roam_objective",
                "target": "<redacted>",
            }
        )
        source = RoamSource(
            objective_present=True,
            objective_commitment=objective_commitment,
        )
        config = _commitment(
            _configuration_shape(
                execution_kind=ExecutionKind.ROAM,
                provider=provider,
                adapter=adapter,
                extra={"objective": "<redacted>"},
            )
        )
        run_record = RunRecord(
            run_id=RunId.new(),
            execution_kind=ExecutionKind.ROAM,
            source=source,
            started_at=_utc_now(),
            argus_version=__version__,
            adapter_type=_adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=_RUNTIME_PROFILE,
            configuration_commitment=config,
            provider=_provider_type(provider),
        )
        roam_step = StepRecord(
            step_id=StepId.new(),
            instruction=EvidenceValue.suppressed(_TARGET_REASON),
            kind="roam",
        )
        return cls(
            project_dir,
            run_record,
            (roam_step,),
            environment,
            roam_step=roam_step,
        )

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def failure(self) -> Optional[BaseException]:
        return self._failure

    @property
    def current_attempt_id(self) -> Optional[StepAttemptId]:
        return self._current.attempt_id if self._current else None

    @property
    def current_step_id(self) -> Optional[StepId]:
        return self._current.step.step_id if self._current else None

    def _append(self, event_type: EventType, payload: Mapping[str, object]) -> None:
        if self._failed:
            raise AtesRuntimeError("ATES runtime recorder is unavailable after a prior failure")
        try:
            self._store.append(event_type, payload)  # type: ignore[arg-type]
        except BaseException as exc:
            self._failed = True
            self._failure = exc
            raise

    def close(self) -> None:
        self._store.close()

    def target_launched(self) -> None:
        self._append(
            EventType.TARGET_LAUNCHED,
            {"target": to_json_compatible(EvidenceValue.suppressed(_TARGET_REASON))},
        )

    def target_closed(self) -> None:
        self._append(EventType.TARGET_CLOSED, {})

    def environment_released(self) -> None:
        self._append(EventType.ENVIRONMENT_RELEASED, {})

    def failure_capsule_retained(self) -> None:
        self._append(EventType.FAILURE_CAPSULE_RETAINED, {"retained": True})

    def begin_step(
        self,
        index: int,
        ordinal: int = 1,
        *,
        attempt_id: Optional[StepAttemptId] = None,
        retry: bool = False,
    ) -> StepAttemptId:
        if self._current is not None:
            raise AtesRuntimeError("cannot begin a new step attempt while another is active")
        try:
            step = self.steps[index]
        except IndexError as exc:
            raise AtesRuntimeError(f"unknown scripted step index: {index}") from exc
        return self._begin(step, ordinal, attempt_id=attempt_id, retry=retry)

    def begin_roam(self) -> StepAttemptId:
        if self._roam_step is None:
            raise AtesRuntimeError("this recorder does not describe a roam execution")
        if self._current is not None:
            raise AtesRuntimeError("roam attempt already active")
        return self._begin(self._roam_step, 1)

    def _begin(
        self,
        step: StepRecord,
        ordinal: int,
        *,
        attempt_id: Optional[StepAttemptId] = None,
        retry: bool = False,
    ) -> StepAttemptId:
        chosen_id = attempt_id or StepAttemptId.new()
        retry_reason = EvidenceValue.suppressed(_RETRY_REASON) if retry else None
        started_at = _utc_now()
        record = StepAttemptRecord(
            step_attempt_id=chosen_id,
            step_id=step.step_id,
            attempt=ordinal,
            status=StepAttemptStatus.RUNNING,
            started_at=started_at,
            retry_reason=retry_reason,
        )
        self._append(
            EventType.STEP_ATTEMPT_STARTED,
            {"attempt": to_json_compatible(record)},
        )
        self._current = _AttemptContext(
            step=step,
            attempt_id=chosen_id,
            ordinal=ordinal,
            started_at=started_at,
            retry_reason=retry_reason,
        )
        self._latest_observation_id = None
        return chosen_id

    def schedule_retry(
        self,
        index: int,
        previous_attempt_id: StepAttemptId,
        next_ordinal: int,
    ) -> StepAttemptId:
        if self._current is not None:
            raise AtesRuntimeError("complete the prior attempt before scheduling a retry")
        step = self.steps[index]
        next_id = StepAttemptId.new()
        self._append(
            EventType.STEP_RETRY_SCHEDULED,
            {
                "step_id": str(step.step_id),
                "previous_step_attempt_id": str(previous_attempt_id),
                "next_step_attempt_id": str(next_id),
                "next_attempt": int(next_ordinal),
                "reason": to_json_compatible(EvidenceValue.suppressed(_RETRY_REASON)),
            },
        )
        return next_id

    def complete_current(self, status: str) -> Optional[StepAttemptId]:
        current = self._current
        if current is None:
            return None
        record = StepAttemptRecord(
            step_attempt_id=current.attempt_id,
            step_id=current.step.step_id,
            attempt=current.ordinal,
            status=_step_status(status),
            started_at=current.started_at,
            ended_at=_utc_now(),
            retry_reason=current.retry_reason,
        )
        self._append(
            EventType.STEP_ATTEMPT_COMPLETED,
            {"attempt": to_json_compatible(record)},
        )
        self._current = None
        self._latest_observation_id = None
        return current.attempt_id

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
            "window_title": EvidenceValue.suppressed(_PRIVACY_REASON),
            "target_text": EvidenceValue.suppressed(_PRIVACY_REASON),
        }
        record = ObservationRecord(
            observation_id=ObservationId.new(),
            step_attempt_id=current.attempt_id,
            source=source,
            captured_at=_utc_now(),
            capture_policy=_RUNTIME_PROFILE,
            facts=facts,
        )
        self._append(
            EventType.OBSERVATION_CAPTURED,
            {"observation": to_json_compatible(record)},
        )
        self._latest_observation_id = record.observation_id
        return record.observation_id

    def record_action_proposed(self, action: Mapping[str, object]) -> ActionRecord:
        current = self._current
        if current is None:
            raise AtesRuntimeError("action occurred outside an active step attempt")
        kind = str(action.get("action") or "").strip()
        if not kind:
            raise AtesRuntimeError("action proposal has no action kind")
        parameters = {
            str(key): EvidenceValue.suppressed(_ACTION_REASON)
            for key in action
            if key != "action"
        }
        record = ActionRecord(
            action_id=ActionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            action_type=kind,
            parameters=parameters,
            operation_id=ActionOperationId.new(),
        )
        self._append(
            EventType.ACTION_PROPOSED,
            {"action": to_json_compatible(record)},
        )
        return record

    def record_action_executed(self, action: ActionRecord) -> None:
        self._append(
            EventType.ACTION_EXECUTED,
            {
                "action_id": str(action.action_id),
                "operation_id": str(action.operation_id) if action.operation_id else None,
                "result": "executed",
            },
        )

    def record_action_outcome_unknown(self, action: ActionRecord) -> None:
        self._append(
            EventType.ACTION_OUTCOME_UNKNOWN,
            {
                "action_id": str(action.action_id),
                "operation_id": str(action.operation_id) if action.operation_id else None,
                "error": to_json_compatible(EvidenceValue.suppressed(_PRIVACY_REASON)),
            },
        )

    def record_assertion(self, step: AssertStep, status: str, actual_present: bool) -> AssertionId:
        current = self._current
        if current is None:
            raise AtesRuntimeError("assertion occurred outside an active step attempt")
        record = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            kind=step.assertion,
            expected=EvidenceValue.suppressed(_ASSERTION_REASON),
            result=_assertion_result(status),
            method="deterministic.adapter_observation",
            observation_id=self._latest_observation_id,
            actual=(
                EvidenceValue.suppressed(_ASSERTION_REASON)
                if actual_present
                else None
            ),
            required=True,
        )
        self._append(
            EventType.ASSERTION_EVALUATED,
            {"assertion": to_json_compatible(record)},
        )
        return record.assertion_id

    def record_finding(self, *, source: str, classification: str) -> FindingId:
        refs: tuple[str, ...] = ()
        if self._latest_observation_id is not None:
            refs = (str(self._latest_observation_id),)
        record = FindingRecord(
            finding_id=FindingId.new(),
            title=EvidenceValue.suppressed(_FINDING_REASON),
            description=EvidenceValue.suppressed(_FINDING_REASON),
            evidence_refs=refs,
            classification_source=str(source or "runtime"),
            classification=str(classification or "unclassified"),
        )
        self._append(
            EventType.FINDING_RECORDED,
            {"finding": to_json_compatible(record)},
        )
        return record.finding_id

    def mark_incomplete(self, reason: str, *, execution_result: Optional[str] = None) -> None:
        payload: dict[str, object] = {"reason": str(reason)}
        if execution_result is not None:
            payload["execution_result"] = str(execution_result)
        self._append(EventType.RUN_MARKED_INCOMPLETE, payload)


class AtesAdapterProxy(Adapter):
    """Observe the existing Adapter boundary without changing its semantics."""

    def __init__(self, inner: Adapter, recorder: AtesRuntimeRecorder) -> None:
        self.inner = inner
        self.recorder = recorder
        self.type_name = getattr(inner, "type_name", "adapter")
        self._launched = False
        self._closed_event_emitted = False

    def launch(self, target: str) -> None:
        self.inner.launch(target)
        self._launched = True
        self.recorder.target_launched()

    def observe(self, include_screenshot: bool = True) -> Observation:
        obs = self.inner.observe(include_screenshot=include_screenshot)
        if self.recorder.current_attempt_id is not None and not self.recorder.failed:
            self.recorder.record_observation(obs, self.type_name)
        return obs

    def capabilities(self) -> dict:
        return self.inner.capabilities()

    def validate_action(self, action: dict) -> None:
        self.inner.validate_action(action)

    def act(self, action: dict) -> str:
        proposed = self.recorder.record_action_proposed(action)
        try:
            note = self.inner.act(action)
        except BaseException as exc:
            if not self.recorder.failed:
                try:
                    self.recorder.record_action_outcome_unknown(proposed)
                except BaseException as evidence_exc:
                    raise evidence_exc from exc
            raise
        self.recorder.record_action_executed(proposed)
        return note

    def close(self) -> None:
        self.inner.close()
        if (
            self._launched
            and not self._closed_event_emitted
            and not self.recorder.failed
        ):
            self.recorder.target_closed()
            self._closed_event_emitted = True

    def __getattr__(self, name):
        return getattr(self.inner, name)
