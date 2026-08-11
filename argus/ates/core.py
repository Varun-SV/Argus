"""Core ATES schema primitives."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Optional, Sequence, TypeVar, Union

from .ids import (
    ActionId, ActionOperationId, ArtifactId, AssertionId, AtesId, CorrectionId,
    EventId, FinalizationId, FindingId, ObservationId, RunId, StepAttemptId, StepId,
)

ATES_VERSION = "0.1"
STATUS_POLICY_VERSION = "ates-status-v1"
SUPPORTED_STATUS_POLICY_VERSIONS = frozenset({STATUS_POLICY_VERSION})
RUN_OUTCOME_REFINALIZATION_SCOPE = "run_outcome.refinalize"
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class ExecutionKind(str, Enum):
    SCRIPTED = "scripted"
    ROAM = "roam"


class RunStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"


class LifecycleState(str, Enum):
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"


class StepAttemptStatus(str, Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AssertionResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    UNEVALUATED = "unevaluated"


class EvidenceDisposition(str, Enum):
    SAFE = "safe"
    REDACTED = "redacted"
    SUPPRESSED = "suppressed"
    PROTECTED_REF = "protected_ref"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    INVALID = "invalid"


class EventType(str, Enum):
    RUN_STARTED = "RUN_STARTED"
    ENVIRONMENT_PREPARED = "ENVIRONMENT_PREPARED"
    CAPSULE_CREATED = "CAPSULE_CREATED"
    TARGET_LAUNCHED = "TARGET_LAUNCHED"
    STEP_ATTEMPT_STARTED = "STEP_ATTEMPT_STARTED"
    OBSERVATION_CAPTURED = "OBSERVATION_CAPTURED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTION_POLICY_VALIDATED = "ACTION_POLICY_VALIDATED"
    ACTION_DISPATCH_COMMITTED = "ACTION_DISPATCH_COMMITTED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    ACTION_OUTCOME_UNKNOWN = "ACTION_OUTCOME_UNKNOWN"
    ASSERTION_EVALUATED = "ASSERTION_EVALUATED"
    STEP_ATTEMPT_COMPLETED = "STEP_ATTEMPT_COMPLETED"
    STEP_RETRY_SCHEDULED = "STEP_RETRY_SCHEDULED"
    CHECKPOINT_CAPTURED = "CHECKPOINT_CAPTURED"
    ARTIFACT_SUPPRESSED = "ARTIFACT_SUPPRESSED"
    FINDING_RECORDED = "FINDING_RECORDED"
    ARTIFACT_COLLECTED = "ARTIFACT_COLLECTED"
    TARGET_CLOSED = "TARGET_CLOSED"
    FAILURE_CAPSULE_RETAINED = "FAILURE_CAPSULE_RETAINED"
    ENVIRONMENT_RELEASED = "ENVIRONMENT_RELEASED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_MARKED_INCOMPLETE = "RUN_MARKED_INCOMPLETE"
    SEQUENCE_TOMBSTONE = "SEQUENCE_TOMBSTONE"


JsonScalar = Union[str, int, float, bool, None]
JsonValue = Union[JsonScalar, Sequence["JsonValue"], Mapping[str, "JsonValue"]]
TEnum = TypeVar("TEnum", bound=Enum)
TAtesId = TypeVar("TAtesId", bound=AtesId)


class FrozenDict(Mapping[str, object]):
    """Truly immutable mapping backed only by an immutable tuple of entries."""

    __slots__ = ("_items",)

    def __init__(
        self,
        source: Mapping[str, object] | Sequence[tuple[str, object]] = (),
    ) -> None:
        items = tuple(source.items()) if isinstance(source, Mapping) else tuple(source)
        seen: set[str] = set()
        for key, _ in items:
            if not isinstance(key, str):
                raise TypeError("FrozenDict keys must be strings")
            if key in seen:
                raise ValueError("FrozenDict keys must be unique")
            seen.add(key)
        object.__setattr__(self, "_items", items)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("FrozenDict is immutable")

    def __delattr__(self, name: str) -> None:
        raise TypeError("FrozenDict is immutable")

    def __getitem__(self, key: str) -> object:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self):
        return self._items

    def __deepcopy__(self, memo: dict[int, object]):
        # dataclasses.asdict() receives a detached mutable copy; the canonical
        # FrozenDict instance itself remains immutable. to_json_compatible() is
        # the explicit supported serialization boundary.
        return {key: deepcopy(value, memo) for key, value in self._items}

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._items)!r})"


def _snapshot_evidence_mapping(value: object, field_name: str) -> FrozenDict:
    """Snapshot once, then validate and retain exactly that evidence mapping."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must map strings to EvidenceValue")
    try:
        snapshot = tuple(value.items())
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} could not be snapshotted safely") from exc
    if not all(
        isinstance(key, str) and isinstance(child, EvidenceValue)
        for key, child in snapshot
    ):
        raise ValueError(f"{field_name} must map strings to EvidenceValue")
    if len({key for key, _ in snapshot}) != len(snapshot):
        raise ValueError(f"{field_name} must not contain duplicate keys")
    return FrozenDict(snapshot)


def _require_nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_reason_code(value: str, field_name: str = "reason") -> str:
    _require_nonempty(value, field_name)
    if not _REASON_CODE_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a safe policy/reason code using lowercase letters, "
            "digits, '.', '_' or '-'"
        )
    return value


def _require_positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _require_nonnegative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _normalize_enum(value: object, enum_type: type[TEnum], field_name: str) -> TEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid {enum_type.__name__}") from exc


def _normalize_id(value: object, id_type: type[TAtesId], field_name: str) -> TAtesId:
    if isinstance(value, id_type):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a valid {id_type.__name__}")
    try:
        return id_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid {id_type.__name__}") from exc


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def _freeze_json(value: JsonValue, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        try:
            snapshot = tuple(value.items())
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError(f"{path} mapping could not be snapshotted safely") from exc
        frozen_items: list[tuple[str, JsonValue]] = []
        seen: set[str] = set()
        for key, child in snapshot:
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            if key in seen:
                raise ValueError(f"{path} contains a duplicate object key")
            seen.add(key)
            frozen_items.append((key, _freeze_json(child, f"{path}.{key}")))
        return FrozenDict(frozen_items)  # type: ignore[return-value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        snapshot = tuple(value)
        return tuple(
            _freeze_json(child, f"{path}[{index}]")
            for index, child in enumerate(snapshot)
        )
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def ensure_json_safe(value: JsonValue, path: str = "$") -> None:
    _freeze_json(value, path)


def freeze_json(value: JsonValue) -> JsonValue:
    """Return a deeply immutable snapshot of a JSON-compatible value."""
    return _freeze_json(value, "$")


def to_json_compatible(value: object) -> JsonValue:
    """Convert ATES Core values to ordinary JSON-compatible containers/scalars."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_json_compatible(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        require_aware_datetime(value, "datetime")
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON output cannot contain a non-finite float")
        return value
    if isinstance(value, Mapping):
        converted: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON output mappings require string keys")
            converted[key] = to_json_compatible(child)
        return converted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_compatible(child) for child in value]
    raise ValueError(f"unsupported ATES JSON value {type(value).__name__}")


@dataclass(frozen=True)
class EvidenceValue:
    disposition: EvidenceDisposition
    value: JsonValue = None
    reason: Optional[str] = None
    secret_refs: tuple[str, ...] = ()
    protected_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disposition",
            _normalize_enum(self.disposition, EvidenceDisposition, "disposition"),
        )
        if isinstance(self.secret_refs, (str, bytes, bytearray)):
            raise ValueError("secret_refs must be a sequence of reference strings")
        object.__setattr__(self, "secret_refs", tuple(self.secret_refs))
        for ref in self.secret_refs:
            _require_nonempty(ref, "secret_ref")

        if self.disposition is EvidenceDisposition.SAFE:
            object.__setattr__(self, "value", freeze_json(self.value))
            if self.reason is not None or self.protected_ref is not None:
                raise ValueError("safe evidence values cannot carry redaction/protected metadata")
            return

        _require_reason_code(self.reason or "")
        if self.disposition is EvidenceDisposition.REDACTED:
            if self.value != "<redacted>":
                raise ValueError("redacted evidence must persist exactly '<redacted>'")
            if self.protected_ref is not None:
                raise ValueError("redacted evidence cannot also be a protected reference")
        elif self.disposition is EvidenceDisposition.SUPPRESSED:
            if self.value is not None or self.protected_ref is not None:
                raise ValueError("suppressed evidence cannot persist a value or protected reference")
        elif self.disposition is EvidenceDisposition.PROTECTED_REF:
            if self.value is not None:
                raise ValueError("protected evidence cannot copy plaintext into ordinary evidence")
            _require_nonempty(self.protected_ref or "", "protected_ref")

    @classmethod
    def safe(cls, value: JsonValue) -> "EvidenceValue":
        return cls(EvidenceDisposition.SAFE, value=value)

    @classmethod
    def redacted(cls, reason: str, *, secret_refs: Sequence[str] = ()) -> "EvidenceValue":
        return cls(
            EvidenceDisposition.REDACTED,
            value="<redacted>",
            reason=reason,
            secret_refs=tuple(secret_refs),
        )

    @classmethod
    def suppressed(cls, reason: str) -> "EvidenceValue":
        return cls(EvidenceDisposition.SUPPRESSED, reason=reason)

    @classmethod
    def protected(cls, protected_ref: str, reason: str) -> "EvidenceValue":
        return cls(
            EvidenceDisposition.PROTECTED_REF,
            reason=reason,
            protected_ref=protected_ref,
        )


@dataclass(frozen=True)
class SourceCommitment:
    method: str
    value: str
    canonicalization_profile: Optional[str] = None
    verification_ref: Optional[str] = None

    def __post_init__(self) -> None:
        _require_nonempty(self.method, "method")
        _require_nonempty(self.value, "value")
        if self.canonicalization_profile is not None:
            _require_nonempty(self.canonicalization_profile, "canonicalization_profile")
        if self.verification_ref is not None:
            _require_nonempty(self.verification_ref, "verification_ref")

    @property
    def identity_key(self) -> tuple[str, str, Optional[str], Optional[str]]:
        return (
            self.method,
            self.value,
            self.canonicalization_profile,
            self.verification_ref,
        )


@dataclass(frozen=True)
class ScriptedSource:
    test_case_id: str
    commitment: SourceCommitment
    kind: str = field(default="test_spec", init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.test_case_id, "test_case_id")
        if not isinstance(self.commitment, SourceCommitment):
            raise ValueError("scripted source commitment must be a SourceCommitment")


@dataclass(frozen=True)
class RoamSource:
    objective_present: bool
    objective_commitment: Optional[SourceCommitment] = None
    config_commitment: Optional[SourceCommitment] = None
    policy_ref: Optional[str] = None
    kind: str = field(default="roam_session", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.objective_present, bool):
            raise ValueError("objective_present must be a boolean")
        for commitment, name in (
            (self.objective_commitment, "objective_commitment"),
            (self.config_commitment, "config_commitment"),
        ):
            if commitment is not None and not isinstance(commitment, SourceCommitment):
                raise ValueError(f"{name} must be a SourceCommitment")
        if self.objective_present and self.objective_commitment is None:
            raise ValueError("roam objective_present=true requires objective_commitment")
        if not self.objective_present and self.objective_commitment is not None:
            raise ValueError("roam objective_present=false forbids objective_commitment")
        if not any((self.objective_commitment, self.config_commitment, self.policy_ref)):
            raise ValueError("roam source requires an objective/config commitment or policy_ref")
        if self.policy_ref is not None:
            _require_nonempty(self.policy_ref, "policy_ref")


RunSource = Union[ScriptedSource, RoamSource]


@dataclass(frozen=True)
class RunRecord:
    run_id: RunId
    execution_kind: ExecutionKind
    source: RunSource
    started_at: datetime
    argus_version: str
    adapter_type: str
    environment_type: str
    evidence_profile: str
    configuration_commitment: SourceCommitment
    provider: Optional[str] = None
    model_provider: Optional[str] = None
    model: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _normalize_id(self.run_id, RunId, "run_id"))
        object.__setattr__(
            self,
            "execution_kind",
            _normalize_enum(self.execution_kind, ExecutionKind, "execution_kind"),
        )
        require_aware_datetime(self.started_at, "started_at")
        _require_nonempty(self.argus_version, "argus_version")
        _require_nonempty(self.adapter_type, "adapter_type")
        _require_nonempty(self.environment_type, "environment_type")
        _require_nonempty(self.evidence_profile, "evidence_profile")
        for value, name in (
            (self.provider, "provider"),
            (self.model_provider, "model_provider"),
            (self.model, "model"),
        ):
            if value is not None:
                _require_nonempty(value, name)
        if not isinstance(self.configuration_commitment, SourceCommitment):
            raise ValueError("configuration_commitment must be a SourceCommitment")
        if self.execution_kind is ExecutionKind.SCRIPTED and not isinstance(self.source, ScriptedSource):
            raise ValueError("scripted runs require ScriptedSource")
        if self.execution_kind is ExecutionKind.ROAM and not isinstance(self.source, RoamSource):
            raise ValueError("roam runs require RoamSource")


@dataclass(frozen=True)
class StepRecord:
    step_id: StepId
    instruction: EvidenceValue
    kind: str = "step"

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_id", _normalize_id(self.step_id, StepId, "step_id"))
        _require_nonempty(self.kind, "kind")
        if not isinstance(self.instruction, EvidenceValue):
            raise ValueError("step instruction must be an EvidenceValue")


@dataclass(frozen=True)
class StepAttemptRecord:
    step_attempt_id: StepAttemptId
    step_id: StepId
    attempt: int
    status: StepAttemptStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    retry_reason: Optional[EvidenceValue] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "step_attempt_id",
            _normalize_id(self.step_attempt_id, StepAttemptId, "step_attempt_id"),
        )
        object.__setattr__(self, "step_id", _normalize_id(self.step_id, StepId, "step_id"))
        _require_positive_int(self.attempt, "attempt")
        object.__setattr__(
            self,
            "status",
            _normalize_enum(self.status, StepAttemptStatus, "step attempt status"),
        )
        require_aware_datetime(self.started_at, "started_at")

        if self.status is StepAttemptStatus.RUNNING:
            if self.ended_at is not None:
                raise ValueError("running attempts cannot have ended_at")
        else:
            if self.ended_at is None:
                raise ValueError("terminal attempts require ended_at")
            require_aware_datetime(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("ended_at cannot precede started_at")

        if self.attempt == 1:
            if self.retry_reason is not None:
                raise ValueError("first attempt cannot have retry_reason")
        else:
            if not isinstance(self.retry_reason, EvidenceValue):
                raise ValueError("retry attempts require retry_reason as EvidenceValue")
            if self.retry_reason.disposition is EvidenceDisposition.SAFE:
                if not isinstance(self.retry_reason.value, str):
                    raise ValueError("safe retry_reason must contain text")
                _require_nonempty(self.retry_reason.value, "retry_reason")


def validate_step_attempt_history(
    attempts: Sequence[StepAttemptRecord],
) -> tuple[StepAttemptRecord, ...]:
    """Validate per-step attempt identity, ordinals, and causal retry ordering."""
    if isinstance(attempts, (str, bytes, bytearray)):
        raise ValueError("attempt history must be a sequence of StepAttemptRecord values")
    snapshot = tuple(attempts)
    histories: dict[StepId, list[StepAttemptRecord]] = {}
    seen_attempt_ids: set[StepAttemptId] = set()
    for item in snapshot:
        if not isinstance(item, StepAttemptRecord):
            raise ValueError("attempt history must contain only StepAttemptRecord values")
        if item.step_attempt_id in seen_attempt_ids:
            raise ValueError("step attempt IDs must be unique across an attempt history")
        seen_attempt_ids.add(item.step_attempt_id)
        histories.setdefault(item.step_id, []).append(item)

    for step_id, history in histories.items():
        ordered = sorted(history, key=lambda item: item.attempt)
        ordinals = [item.attempt for item in ordered]
        expected = list(range(1, len(ordered) + 1))
        if ordinals != expected:
            raise ValueError(
                f"attempt ordinals for {step_id} must be unique and contiguous starting at 1"
            )
        for previous, current in zip(ordered, ordered[1:]):
            if previous.status is StepAttemptStatus.RUNNING or previous.ended_at is None:
                raise ValueError("a retry cannot start before the prior attempt is terminal")
            if previous.ended_at > current.started_at:
                raise ValueError("retry attempts cannot overlap or move backward in time")
    return snapshot


@dataclass(frozen=True)
class ActionRecord:
    action_id: ActionId
    step_id: StepId
    step_attempt_id: StepAttemptId
    action_type: str
    parameters: Mapping[str, EvidenceValue] = field(default_factory=dict)
    operation_id: Optional[ActionOperationId] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _normalize_id(self.action_id, ActionId, "action_id"))
        object.__setattr__(self, "step_id", _normalize_id(self.step_id, StepId, "step_id"))
        object.__setattr__(
            self,
            "step_attempt_id",
            _normalize_id(self.step_attempt_id, StepAttemptId, "step_attempt_id"),
        )
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                _normalize_id(self.operation_id, ActionOperationId, "operation_id"),
            )
        _require_nonempty(self.action_type, "action_type")
        object.__setattr__(
            self,
            "parameters",
            _snapshot_evidence_mapping(self.parameters, "action parameters"),
        )


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: ObservationId
    step_attempt_id: StepAttemptId
    source: str
    captured_at: datetime
    capture_policy: str
    facts: Mapping[str, EvidenceValue]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _normalize_id(self.observation_id, ObservationId, "observation_id"),
        )
        object.__setattr__(
            self,
            "step_attempt_id",
            _normalize_id(self.step_attempt_id, StepAttemptId, "step_attempt_id"),
        )
        require_aware_datetime(self.captured_at, "captured_at")
        _require_nonempty(self.source, "source")
        _require_nonempty(self.capture_policy, "capture_policy")
        object.__setattr__(
            self,
            "facts",
            _snapshot_evidence_mapping(self.facts, "observation facts"),
        )


@dataclass(frozen=True)
class AssertionRecord:
    assertion_id: AssertionId
    step_id: StepId
    step_attempt_id: StepAttemptId
    kind: str
    expected: EvidenceValue
    result: AssertionResult
    method: str
    observation_id: Optional[ObservationId] = None
    actual: Optional[EvidenceValue] = None
    required: bool = True
    requirement: Optional["RequirementIdentity"] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assertion_id",
            _normalize_id(self.assertion_id, AssertionId, "assertion_id"),
        )
        object.__setattr__(self, "step_id", _normalize_id(self.step_id, StepId, "step_id"))
        object.__setattr__(
            self,
            "step_attempt_id",
            _normalize_id(self.step_attempt_id, StepAttemptId, "step_attempt_id"),
        )
        if self.observation_id is not None:
            object.__setattr__(
                self,
                "observation_id",
                _normalize_id(self.observation_id, ObservationId, "observation_id"),
            )
        _require_nonempty(self.kind, "kind")
        _require_nonempty(self.method, "method")
        if not isinstance(self.expected, EvidenceValue):
            raise ValueError("assertion expected value must be an EvidenceValue")
        if self.actual is not None and not isinstance(self.actual, EvidenceValue):
            raise ValueError("assertion actual value must be an EvidenceValue")
        if self.requirement is not None and not isinstance(
            self.requirement, RequirementIdentity
        ):
            raise ValueError("assertion requirement must be a RequirementIdentity")
        object.__setattr__(
            self,
            "result",
            _normalize_enum(self.result, AssertionResult, "assertion result"),
        )
        if not isinstance(self.required, bool):
            raise ValueError("assertion required must be a boolean")


@dataclass(frozen=True)
class FindingRecord:
    finding_id: FindingId
    title: EvidenceValue
    description: EvidenceValue
    evidence_refs: tuple[str, ...] = ()
    classification_source: str = "model"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "finding_id",
            _normalize_id(self.finding_id, FindingId, "finding_id"),
        )
        _require_nonempty(self.classification_source, "classification_source")
        if not isinstance(self.title, EvidenceValue) or not isinstance(self.description, EvidenceValue):
            raise ValueError("finding title and description must be EvidenceValue records")
        if isinstance(self.evidence_refs, (str, bytes, bytearray)):
            raise ValueError("evidence_refs must be a sequence of reference strings")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        for ref in self.evidence_refs:
            _require_nonempty(ref, "evidence_ref")


def validate_artifact_path(path: str) -> str:
    _require_nonempty(path, "artifact path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("artifact path cannot contain control characters")
    if "\\" in path or "%" in path or ":" in path:
        raise ValueError("artifact path must use canonical unencoded POSIX-relative syntax")
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        raise ValueError("artifact path must be relative")
    if candidate.as_posix() != path:
        raise ValueError("artifact path must already be in canonical POSIX-relative form")
    parts = candidate.parts
    if len(parts) < 2 or parts[0] != "artifacts":
        raise ValueError("artifact path must be rooted beneath artifacts/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("artifact path contains an ambiguous/traversal segment")
    return path


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: ArtifactId
    kind: str
    path: str
    sensitivity: str
    capture_policy: str
    content_digest: SourceCommitment
    size_bytes: int
    protection_state: EvidenceDisposition
    protected_ref: Optional[str] = None
    access_policy: Optional[str] = None
    retention_policy: Optional[str] = None
    authorization_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_id",
            _normalize_id(self.artifact_id, ArtifactId, "artifact_id"),
        )
        _require_nonempty(self.kind, "kind")
        _require_nonempty(self.sensitivity, "sensitivity")
        _require_nonempty(self.capture_policy, "capture_policy")
        if not isinstance(self.content_digest, SourceCommitment):
            raise ValueError("content_digest must be a SourceCommitment over final persisted bytes")
        _require_nonnegative_int(self.size_bytes, "size_bytes")
        object.__setattr__(
            self,
            "protection_state",
            _normalize_enum(
                self.protection_state, EvidenceDisposition, "protection_state"
            ),
        )
        if self.protection_state is EvidenceDisposition.SUPPRESSED:
            raise ValueError(
                "suppressed artifacts must use ARTIFACT_SUPPRESSED, not ArtifactRecord"
            )
        protected_metadata = (
            (self.protected_ref, "protected_ref"),
            (self.access_policy, "access_policy"),
            (self.retention_policy, "retention_policy"),
            (self.authorization_ref, "authorization_ref"),
        )
        if self.protection_state is EvidenceDisposition.PROTECTED_REF:
            for value, name in protected_metadata:
                _require_nonempty(value or "", name)
        elif any(value is not None for value, _ in protected_metadata):
            raise ValueError(
                "protected-storage metadata is only valid for protected_ref artifacts"
            )
        validate_artifact_path(self.path)


@dataclass(frozen=True)
class RequirementIdentity:
    requirement_id: str
    source_system: str
    version: Optional[str] = None
    source_revision: Optional[str] = None
    commitment: Optional[SourceCommitment] = None

    def __post_init__(self) -> None:
        _require_nonempty(self.requirement_id, "requirement_id")
        _require_nonempty(self.source_system, "source_system")
        if not any((self.version, self.source_revision, self.commitment)):
            raise ValueError("requirement identity needs version, source_revision, or immutable commitment")
        if self.version is not None:
            _require_nonempty(self.version, "version")
        if self.source_revision is not None:
            _require_nonempty(self.source_revision, "source_revision")
        if self.commitment is not None and not isinstance(self.commitment, SourceCommitment):
            raise ValueError("requirement commitment must be a SourceCommitment")

    @property
    def identity_key(
        self,
    ) -> tuple[
        str,
        str,
        Optional[str],
        Optional[str],
        Optional[tuple[str, str, Optional[str], Optional[str]]],
    ]:
        return (
            self.source_system,
            self.requirement_id,
            self.version,
            self.source_revision,
            self.commitment.identity_key if self.commitment else None,
        )


def validate_step_evidence_relationships(
    attempts: Sequence[StepAttemptRecord],
    *,
    actions: Sequence[ActionRecord] = (),
    observations: Sequence[ObservationRecord] = (),
    assertions: Sequence[AssertionRecord] = (),
) -> tuple[
    tuple[StepAttemptRecord, ...],
    tuple[ActionRecord, ...],
    tuple[ObservationRecord, ...],
    tuple[AssertionRecord, ...],
]:
    """Validate and snapshot foreign-key relationships across step evidence."""
    attempt_snapshot = validate_step_attempt_history(attempts)
    for values, name in (
        (actions, "actions"),
        (observations, "observations"),
        (assertions, "assertions"),
    ):
        if isinstance(values, (str, bytes, bytearray)):
            raise ValueError(f"{name} must be a sequence of ATES records")

    action_snapshot = tuple(actions)
    observation_snapshot = tuple(observations)
    assertion_snapshot = tuple(assertions)
    attempt_owner = {
        attempt.step_attempt_id: attempt.step_id for attempt in attempt_snapshot
    }

    def require_unique_ids(records: Sequence[object], attribute: str, label: str) -> None:
        seen: set[object] = set()
        for record in records:
            identifier = getattr(record, attribute)
            if identifier in seen:
                raise ValueError(f"{label} IDs must be unique in canonical evidence")
            seen.add(identifier)

    seen_operation_ids: set[ActionOperationId] = set()
    for action in action_snapshot:
        if not isinstance(action, ActionRecord):
            raise ValueError("actions must contain only ActionRecord values")
        owner = attempt_owner.get(action.step_attempt_id)
        if owner is None:
            raise ValueError("action references an unknown step_attempt_id")
        if action.step_id != owner:
            raise ValueError("action step_id does not match its step attempt owner")
        if action.operation_id is not None:
            if action.operation_id in seen_operation_ids:
                raise ValueError(
                    "operation IDs must be unique across distinct canonical actions"
                )
            seen_operation_ids.add(action.operation_id)
    require_unique_ids(action_snapshot, "action_id", "action")

    observation_by_id: dict[ObservationId, ObservationRecord] = {}
    for observation in observation_snapshot:
        if not isinstance(observation, ObservationRecord):
            raise ValueError("observations must contain only ObservationRecord values")
        if observation.step_attempt_id not in attempt_owner:
            raise ValueError("observation references an unknown step_attempt_id")
        if observation.observation_id in observation_by_id:
            raise ValueError("observation IDs must be unique in canonical evidence")
        observation_by_id[observation.observation_id] = observation

    for assertion in assertion_snapshot:
        if not isinstance(assertion, AssertionRecord):
            raise ValueError("assertions must contain only AssertionRecord values")
        owner = attempt_owner.get(assertion.step_attempt_id)
        if owner is None:
            raise ValueError("assertion references an unknown step_attempt_id")
        if assertion.step_id != owner:
            raise ValueError("assertion step_id does not match its step attempt owner")
        if assertion.observation_id is not None:
            observation = observation_by_id.get(assertion.observation_id)
            if observation is None:
                raise ValueError("assertion references an unknown observation_id")
            if observation.step_attempt_id != assertion.step_attempt_id:
                raise ValueError(
                    "assertion observation_id must belong to the same step attempt"
                )
    require_unique_ids(assertion_snapshot, "assertion_id", "assertion")

    return (
        attempt_snapshot,
        action_snapshot,
        observation_snapshot,
        assertion_snapshot,
    )


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    method: Optional[str] = None
    actor: Optional[str] = None
    binding_ref: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _normalize_enum(self.status, VerificationStatus, "verification status"),
        )
        if self.status is VerificationStatus.VERIFIED:
            _require_nonempty(self.method or "", "verification method")
            _require_nonempty(self.actor or "", "verified actor")
            _require_nonempty(self.binding_ref or "", "binding_ref")
        for value, name in ((self.method, "verification method"), (self.actor, "actor"), (self.binding_ref, "binding_ref")):
            if value is not None:
                _require_nonempty(value, name)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    scope: str
    policy_id: str
    policy_version: str
    decision_ref: str
    verification: Verification
    subject: str
    run_id: RunId
    finalization_id: FinalizationId
    supersedes_finalization_id: Optional[FinalizationId]
    correction_ids: tuple[CorrectionId, ...]
    effective_status: RunStatus
    evidence_revision: int
    status_policy_version: str = STATUS_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("authorization allowed must be a boolean")
        _require_nonempty(self.scope, "authorization scope")
        _require_nonempty(self.policy_id, "authorization policy_id")
        _require_nonempty(self.policy_version, "authorization policy_version")
        _require_nonempty(self.decision_ref, "authorization decision_ref")
        _require_nonempty(self.subject, "authorization subject")
        if not isinstance(self.verification, Verification):
            raise ValueError("authorization verification must be a validated Verification instance")
        if self.verification.status is not VerificationStatus.VERIFIED:
            raise ValueError("authorization decision must itself be verified")
        object.__setattr__(self, "run_id", _normalize_id(self.run_id, RunId, "authorization run_id"))
        object.__setattr__(
            self,
            "finalization_id",
            _normalize_id(self.finalization_id, FinalizationId, "authorization finalization_id"),
        )
        if self.supersedes_finalization_id is not None:
            object.__setattr__(
                self,
                "supersedes_finalization_id",
                _normalize_id(
                    self.supersedes_finalization_id,
                    FinalizationId,
                    "authorization supersedes_finalization_id",
                ),
            )
        if isinstance(self.correction_ids, (str, bytes, bytearray)):
            raise ValueError("authorization correction_ids must be a sequence of CorrectionId values")
        normalized_corrections = tuple(
            _normalize_id(item, CorrectionId, "authorization correction_id")
            for item in self.correction_ids
        )
        if len(set(normalized_corrections)) != len(normalized_corrections):
            raise ValueError("authorization correction_ids must be unique")
        object.__setattr__(self, "correction_ids", normalized_corrections)
        object.__setattr__(
            self,
            "effective_status",
            _normalize_enum(self.effective_status, RunStatus, "authorization effective_status"),
        )
        _require_positive_int(self.evidence_revision, "authorization evidence revision")
        if self.status_policy_version not in SUPPORTED_STATUS_POLICY_VERSIONS:
            raise ValueError(
                f"unsupported authorization status_policy_version: {self.status_policy_version!r}"
            )


@dataclass(frozen=True)
class RunOutcomeRevision:
    finalization_id: FinalizationId
    run_id: RunId
    revision: int
    effective_status: RunStatus
    evidence_revision: int
    finalized_at: datetime
    status_policy_version: str = STATUS_POLICY_VERSION
    supersedes_finalization_id: Optional[FinalizationId] = None
    correction_ids: tuple[CorrectionId, ...] = ()
    verification: Optional[Verification] = None
    authorization: Optional[AuthorizationDecision] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "finalization_id",
            _normalize_id(self.finalization_id, FinalizationId, "finalization_id"),
        )
        object.__setattr__(self, "run_id", _normalize_id(self.run_id, RunId, "run_id"))
        if self.supersedes_finalization_id is not None:
            object.__setattr__(
                self,
                "supersedes_finalization_id",
                _normalize_id(
                    self.supersedes_finalization_id,
                    FinalizationId,
                    "supersedes_finalization_id",
                ),
            )
        if isinstance(self.correction_ids, (str, bytes, bytearray)):
            raise ValueError("correction_ids must be a sequence of CorrectionId values")
        normalized_corrections = tuple(
            _normalize_id(item, CorrectionId, "correction_id")
            for item in self.correction_ids
        )
        if len(set(normalized_corrections)) != len(normalized_corrections):
            raise ValueError("correction_ids must be unique")
        object.__setattr__(self, "correction_ids", normalized_corrections)
        if self.verification is not None and not isinstance(self.verification, Verification):
            raise ValueError("verification must be a validated Verification instance")
        if self.authorization is not None and not isinstance(
            self.authorization, AuthorizationDecision
        ):
            raise ValueError("authorization must be a validated AuthorizationDecision instance")
        _require_positive_int(self.revision, "finalization revision")
        _require_positive_int(self.evidence_revision, "evidence revision")
        object.__setattr__(
            self,
            "effective_status",
            _normalize_enum(self.effective_status, RunStatus, "effective_status"),
        )
        require_aware_datetime(self.finalized_at, "finalized_at")
        if self.status_policy_version not in SUPPORTED_STATUS_POLICY_VERSIONS:
            raise ValueError(
                f"unsupported status_policy_version: {self.status_policy_version!r}"
            )
        if self.revision == 1:
            if self.supersedes_finalization_id is not None or self.correction_ids:
                raise ValueError("original finalization cannot supersede/cite corrections")
        else:
            if self.supersedes_finalization_id is None:
                raise ValueError("re-finalization must identify the superseded finalization")
            if not self.correction_ids:
                raise ValueError("re-finalization requires at least one status-bearing correction")
            if self.verification is None or self.verification.status is not VerificationStatus.VERIFIED:
                raise ValueError("status-bearing re-finalization must be authenticated/verified")
            if self.authorization is None:
                raise ValueError("status-bearing re-finalization requires an authorization decision")
            if not self.authorization.allowed:
                raise ValueError("status-bearing re-finalization is not authorized")
            if self.authorization.scope != RUN_OUTCOME_REFINALIZATION_SCOPE:
                raise ValueError(
                    "authorization decision does not cover run-outcome re-finalization"
                )
            if self.authorization.subject != self.verification.actor:
                raise ValueError(
                    "authorization subject must match the authenticated re-finalization actor"
                )
            binding_pairs = (
                (self.authorization.run_id, self.run_id, "run_id"),
                (self.authorization.finalization_id, self.finalization_id, "finalization_id"),
                (
                    self.authorization.supersedes_finalization_id,
                    self.supersedes_finalization_id,
                    "supersedes_finalization_id",
                ),
                (self.authorization.correction_ids, self.correction_ids, "correction_ids"),
                (self.authorization.effective_status, self.effective_status, "effective_status"),
                (
                    self.authorization.evidence_revision,
                    self.evidence_revision,
                    "evidence_revision",
                ),
                (
                    self.authorization.status_policy_version,
                    self.status_policy_version,
                    "status_policy_version",
                ),
            )
            for authorized, actual, field_name in binding_pairs:
                if authorized != actual:
                    raise ValueError(
                        f"authorization decision is not bound to this {field_name}"
                    )


@dataclass(frozen=True)
class EventEnvelope:
    ates_version: str
    run_id: RunId
    event_id: EventId
    sequence: int
    event_type: EventType
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.ates_version != ATES_VERSION:
            raise ValueError(f"unsupported ATES version: {self.ates_version!r}")
        object.__setattr__(self, "run_id", _normalize_id(self.run_id, RunId, "run_id"))
        object.__setattr__(self, "event_id", _normalize_id(self.event_id, EventId, "event_id"))
        _require_positive_int(self.sequence, "event sequence")
        object.__setattr__(
            self,
            "event_type",
            _normalize_enum(self.event_type, EventType, "event_type"),
        )
        require_aware_datetime(self.occurred_at, "occurred_at")
