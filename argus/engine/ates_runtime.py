"""Runtime glue between Argus execution engines and canonical ATES evidence.

Runtime objects are projected into ATES through a versioned privacy policy
before ordinary evidence is persisted.  The executable objects themselves are
never mutated for logging/redaction purposes.  Binary screenshots and files
remain outside this module and are handled by the dedicated artifact pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence

from argus import __version__
from argus.adapters.base import Adapter, AdapterError, Observation
from argus.ates import (
    ActionId,
    ActionOperationId,
    ActionRecord,
    AssertionId,
    AssertionRecord,
    AssertionResult,
    AtesEventStore,
    EventType,
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
    SourceCommitment,
    StepAttemptId,
    StepAttemptRecord,
    StepAttemptStatus,
    StepId,
    StepRecord,
    to_json_compatible,
)
from argus.engine.spec import AssertStep, TestSpec

_IDENTITY_KEY_FILENAME = ".ates-runtime-identity.key"
_IDENTITY_KEY_SIZE = 32
_IDENTITY_VERIFICATION_REF = "protected://ates-runtime-identity-key"
_SAFE_FINDING_CLASSIFICATIONS = frozenset({"low", "medium", "high", "critical"})
_SAFE_FINDING_SOURCES = frozenset({"model", "crash", "dialog", "hang", "runtime"})
_ACTION_PARAMETER_KEYS: dict[str, tuple[str, ...]] = {
    "click": ("element_id", "x", "y"),
    "double_click": ("element_id", "x", "y"),
    "right_click": ("element_id", "x", "y"),
    "type": ("text", "element_id"),
    "key": ("keys",),
    "scroll": ("direction", "amount"),
    "menu": ("path",),
    "wait": ("seconds",),
    "done": ("success",),
    "navigate": ("url",),
    "run": ("command",),
    "execute": ("command",),
    "report_bug": ("title", "severity", "expected", "actual", "why"),
}


class AtesRuntimeError(RuntimeError):
    """Raised when mandatory runtime evidence cannot be represented safely."""


class ActionOutcomeUnresolvedError(AdapterError):
    """Raised when a committed side effect has no trusted terminal outcome."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _commitment(value: object) -> SourceCommitment:
    """Create a public commitment over an already-redacted/safe structure."""
    return SourceCommitment(
        method="sha256_redacted_canonical",
        value="sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest(),
        canonicalization_profile="ates-runtime-redacted-v1",
    )


def _identity_key(project_dir: Path) -> bytes:
    """Load or atomically create the project-local protected identity key.

    Runtime source/model commitments must distinguish real inputs without
    publishing dictionary-checkable hashes of low-entropy secrets.  The key is
    therefore kept beside ignored runtime evidence, never inside canonical
    ATES JSONL.  A corrupt/partial key fails closed instead of weakening the
    commitment silently.
    """
    project_root = Path(project_dir).resolve(strict=True)
    key_dir = project_root / ".argus" / "runs"
    key_dir.mkdir(parents=True, exist_ok=True)
    if key_dir.resolve(strict=True).parents[1] != project_root:
        raise AtesRuntimeError("ATES runtime identity-key directory escapes project root")
    key_path = key_dir / _IDENTITY_KEY_FILENAME
    if key_path.is_symlink():
        raise AtesRuntimeError("ATES runtime identity key must not be a symlink")

    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        candidate = os.urandom(_IDENTITY_KEY_SIZE)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(key_path, flags, 0o600)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(candidate)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    key_path.unlink()
                except OSError:
                    pass
                raise
            key = candidate
            try:
                key_path.chmod(0o600)
            except OSError:
                # Windows ACLs do not map cleanly to POSIX chmod; the file is
                # still kept outside canonical evidence and ignored by git.
                pass

    if len(key) != _IDENTITY_KEY_SIZE:
        raise AtesRuntimeError("ATES runtime identity key is invalid or incomplete")
    return key


def _protected_commitment(
    key: bytes,
    value: object,
    *,
    profile: str,
) -> SourceCommitment:
    digest = hmac.new(key, _canonical_bytes(value), hashlib.sha256).hexdigest()
    return SourceCommitment(
        method="hmac-sha256",
        value="hmac:" + digest,
        canonicalization_profile=profile,
        verification_ref=_IDENTITY_VERIFICATION_REF,
    )


def _opaque_identity(key: bytes, namespace: str, value: object) -> str:
    digest = hmac.new(
        key,
        _canonical_bytes({"namespace": namespace, "value": value}),
        hashlib.sha256,
    ).hexdigest()
    return digest[:24]


def _provider_type(provider) -> str:
    value = getattr(provider, "type_name", None) or type(provider).__name__
    return str(value).strip() or "unknown-provider"


def _provider_model(provider) -> str:
    value = getattr(provider, "model", None)
    return str(value).strip() if value is not None and str(value).strip() else "unknown-model"


def _model_identity(key: bytes, provider) -> str:
    return "MODEL-" + _opaque_identity(key, "model", _provider_model(provider))


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
    """Canonical in-memory source shape; never persisted in plaintext."""
    steps: list[dict[str, object]] = []
    for step in spec.steps:
        if isinstance(step, AssertStep):
            steps.append(
                {
                    "kind": step.kind,
                    "type": "assertion",
                    "assertion": step.assertion,
                    "expected": step.expected,
                }
            )
        else:
            steps.append(
                {
                    "kind": step.kind,
                    "type": "natural_language",
                    "text": step.text,
                }
            )
    return {
        "kind": "test_spec",
        "name": spec.name,
        "adapter": spec.adapter,
        "launch": spec.launch,
        "steps": steps,
        "continue_on_failure": bool(spec.continue_on_failure),
        "retries": int(spec.retries),
        "staging": [
            {
                "source": item.source,
                "destination": item.destination,
                "sha256": item.sha256,
            }
            for item in spec.staging
        ],
        "collect": list(spec.collect),
    }


def _scripted_test_identity_shape(project_dir: Path, spec: TestSpec) -> dict[str, object]:
    path_identity: Optional[str] = None
    if spec.path is not None:
        path = Path(spec.path)
        try:
            path_identity = path.resolve().relative_to(Path(project_dir).resolve()).as_posix()
        except (OSError, ValueError):
            path_identity = path.name
    return {
        "kind": "test_case",
        "name": spec.name,
        "path": path_identity,
    }


def _configuration_shape(
    *,
    execution_kind: ExecutionKind,
    provider,
    adapter: Adapter,
    model_identity: str,
    extra: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    environment = _environment_shape(adapter)
    value: dict[str, object] = {
        "execution_kind": execution_kind.value,
        "provider_type": _provider_type(provider),
        "model_identity": model_identity,
        "adapter_type": _adapter_type(adapter),
        **environment,
    }
    if extra:
        value.update(dict(extra))
    return value


def _safe_action_structure(action: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
    """Return only allow-listed model-independent action structure."""
    raw_kind = action.get("action")
    kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
    allowed = _ACTION_PARAMETER_KEYS.get(kind)
    if allowed is None:
        return "invalid", ()
    return kind, tuple(key for key in allowed if key in action)


def _safe_finding_source(value: str) -> str:
    candidate = str(value or "runtime").strip().lower()
    return candidate if candidate in _SAFE_FINDING_SOURCES else "runtime"


def _safe_finding_classification(value: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in _SAFE_FINDING_CLASSIFICATIONS else "unclassified"


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
    """Durably emit privacy-classified ATES runtime lifecycle evidence."""

    def __init__(
        self,
        project_dir: Path,
        run_record: RunRecord,
        steps: Sequence[StepRecord],
        environment: Mapping[str, object],
        *,
        roam_step: Optional[StepRecord] = None,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
        target_value: Optional[str] = None,
    ) -> None:
        self.privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        self._target_value = target_value
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
        *,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
    ) -> "AtesRuntimeRecorder":
        privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        identity_key = _identity_key(project_dir)
        source_commitment = _protected_commitment(
            identity_key,
            _scripted_source_shape(spec),
            profile="ates-runtime-scripted-source-v1",
        )
        test_case_id = "TEST-" + _opaque_identity(
            identity_key,
            "test-case",
            _scripted_test_identity_shape(project_dir, spec),
        )
        source = ScriptedSource(
            test_case_id=test_case_id,
            commitment=source_commitment,
        )
        environment = _environment_shape(adapter)
        model_identity = _model_identity(identity_key, provider)
        config = _protected_commitment(
            identity_key,
            _configuration_shape(
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
            started_at=_utc_now(),
            argus_version=__version__,
            adapter_type=_adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=privacy.policy_id,
            configuration_commitment=config,
            provider=_provider_type(provider),
            model_provider=_provider_type(provider),
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
        project_dir: Path,
        provider,
        adapter: Adapter,
        *,
        target: str,
        privacy_policy: Optional[EvidencePrivacyPolicy] = None,
    ) -> "AtesRuntimeRecorder":
        privacy = privacy_policy or EvidencePrivacyPolicy.standard()
        identity_key = _identity_key(project_dir)
        environment = _environment_shape(adapter)
        model_identity = _model_identity(identity_key, provider)
        target_identity = "TARGET-" + _opaque_identity(identity_key, "roam-target", target)
        source_config = _protected_commitment(
            identity_key,
            {
                "kind": "roam_session",
                "target": target,
                "provider_type": _provider_type(provider),
                "provider_model": _provider_model(provider),
                "adapter_type": _adapter_type(adapter),
                "privacy_policy": privacy.policy_id,
            },
            profile="ates-runtime-roam-source-v1",
        )
        source = RoamSource(
            objective_present=False,
            config_commitment=source_config,
        )
        config = _protected_commitment(
            identity_key,
            _configuration_shape(
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
            started_at=_utc_now(),
            argus_version=__version__,
            adapter_type=_adapter_type(adapter),
            environment_type=str(environment["environment_type"]),
            evidence_profile=privacy.policy_id,
            configuration_commitment=config,
            provider=_provider_type(provider),
            model_provider=_provider_type(provider),
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
            {
                "target": to_json_compatible(
                    self.privacy.capture(
                        self._target_value,
                        context=EvidenceContext.TARGET,
                        field_name="target",
                    )
                )
            },
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
        retry_reason = (
            self.privacy.capture(
                "retry",
                context=EvidenceContext.RETRY_REASON,
                field_name="reason",
            )
            if retry
            else None
        )
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
                "reason": to_json_compatible(
                    self.privacy.capture(
                        "retry",
                        context=EvidenceContext.RETRY_REASON,
                        field_name="reason",
                    )
                ),
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
            captured_at=_utc_now(),
            capture_policy=self.privacy.policy_id,
            facts=facts,
        )
        self._append(
            EventType.OBSERVATION_CAPTURED,
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
        kind, parameter_keys = _safe_action_structure(values)
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
        kind, _ = _safe_action_structure(action)
        seed = ActionRecord(
            action_id=ActionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            action_type=kind,
            parameters={},
            operation_id=ActionOperationId.new(),
        )
        record = self._project_action_record(seed, action, validated=False)
        self._append(
            EventType.ACTION_PROPOSED,
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
            EventType.ACTION_POLICY_VALIDATED,
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
            EventType.ACTION_DISPATCH_COMMITTED,
            {"action": to_json_compatible(committed)},
        )
        return committed

    def record_action_executed(self, action: ActionRecord) -> None:
        self._append(
            EventType.ACTION_EXECUTED,
            {
                "action_id": str(action.action_id),
                "operation_id": str(action.operation_id) if action.operation_id else None,
                "result": "executed",
            },
        )

    def record_action_outcome_unknown(
        self,
        action: ActionRecord,
        error: object = None,
    ) -> None:
        self._append(
            EventType.ACTION_OUTCOME_UNKNOWN,
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
            actual = self.privacy.assertion_actual(
                actual_value if actual_value is not None else "<present>"
            )
        record = AssertionRecord(
            assertion_id=AssertionId.new(),
            step_id=current.step.step_id,
            step_attempt_id=current.attempt_id,
            kind=step.assertion,
            expected=self.privacy.assertion_expected(step.expected),
            result=_assertion_result(status),
            method="deterministic.adapter_observation",
            observation_id=self._latest_observation_id,
            actual=actual,
            required=True,
        )
        self._append(
            EventType.ASSERTION_EVALUATED,
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
            classification_source=_safe_finding_source(source),
            classification=_safe_finding_classification(classification),
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
    """Bind real Adapter dispatch to durable, privacy-classified ATES evidence."""

    def __init__(self, inner: Adapter, recorder: AtesRuntimeRecorder) -> None:
        self.inner = inner
        self.recorder = recorder
        self.type_name = getattr(inner, "type_name", "adapter")
        self._launched = False
        self._closed_event_emitted = False
        self._unresolved_action: Optional[ActionRecord] = None

    @property
    def unresolved_operation_id(self) -> Optional[ActionOperationId]:
        if self._unresolved_action is None:
            return None
        return self._unresolved_action.operation_id

    def _ensure_no_unresolved_dispatch(self) -> None:
        unresolved = self._unresolved_action
        if unresolved is None:
            return
        operation_id = unresolved.operation_id
        raise ActionOutcomeUnresolvedError(
            "action outcome unresolved after durable dispatch commit"
            + (f" ({operation_id})" if operation_id else "")
            + "; refusing further target interaction until reconciled"
        )

    def launch(self, target: str) -> None:
        self.inner.launch(target)
        self._launched = True
        self.recorder.target_launched()

    def observe(self, include_screenshot: bool = True) -> Observation:
        self._ensure_no_unresolved_dispatch()
        obs = self.inner.observe(include_screenshot=include_screenshot)
        if self.recorder.current_attempt_id is not None and not self.recorder.failed:
            self.recorder.record_observation(obs, self.type_name)
        return obs

    def capabilities(self) -> dict:
        return self.inner.capabilities()

    def validate_action(self, action: dict) -> None:
        self.inner.validate_action(action)

    def act(self, action: dict) -> str:
        self._ensure_no_unresolved_dispatch()
        proposed = self.recorder.record_action_proposed(action)

        # No target-visible side effect is permitted before this preparation
        # succeeds. Validation failures therefore remain safely retryable and
        # intentionally do not produce a dispatch-commit event.
        normalized = self.inner.prepare_action(action)
        validated = self.recorder.record_action_policy_validated(proposed, normalized)

        # This append is the durable point of no return. If it fails, dispatch
        # never begins. If it succeeds, recovery must conservatively assume the
        # operation may have reached the target until a terminal outcome proves
        # otherwise.
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
            # The side effect returned successfully but canonical terminal
            # evidence is not durable/known. On recovery a commit without a
            # trusted terminal record is indistinguishable from an ambiguous
            # dispatch, so block all further target interaction.
            self._unresolved_action = committed
            raise
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
