"""Core ATES schema primitives."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Union

from .ids import (
    ActionId, ActionOperationId, ArtifactId, AssertionId, CorrectionId, EventId,
    FinalizationId, FindingId, ObservationId, RunId, StepAttemptId, StepId,
)

ATES_VERSION = "0.1"
STATUS_POLICY_VERSION = "ates-status-v1"


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


def _require_nonempty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")


def ensure_json_safe(value: JsonValue, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            ensure_json_safe(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            ensure_json_safe(child, f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def freeze_json(value: JsonValue) -> JsonValue:
    """Return an immutable copy of a JSON-compatible value."""
    ensure_json_safe(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_json(child) for key, child in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(freeze_json(child) for child in value)
    return value


@dataclass(frozen=True)
class EvidenceValue:
    disposition: EvidenceDisposition
    value: JsonValue = None
    reason: Optional[str] = None
    secret_refs: tuple[str, ...] = ()
    protected_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if self.disposition is EvidenceDisposition.SAFE:
            object.__setattr__(self, "value", freeze_json(self.value))
            if self.reason is not None or self.protected_ref is not None:
                raise ValueError("safe evidence values cannot carry redaction/protected metadata")
            return

        _require_nonempty(self.reason or "", "reason")
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

        for ref in self.secret_refs:
            _require_nonempty(ref, "secret_ref")

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


@dataclass(frozen=True)
class ScriptedSource:
    test_case_id: str
    commitment: SourceCommitment
    kind: str = field(default="test_spec", init=False)

    def __post_init__(self) -> None:
        _require_nonempty(self.test_case_id, "test_case_id")


@dataclass(frozen=True)
class RoamSource:
    objective_present: bool
    objective_commitment: Optional[SourceCommitment] = None
    config_commitment: Optional[SourceCommitment] = None
    policy_ref: Optional[str] = None
    kind: str = field(default="roam_session", init=False)

    def __post_init__(self) -> None:
        if self.objective_present and self.objective_commitment is None:
            raise ValueError("roam objective_present=true requires objective_commitment")
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
    provider: Optional[str] = None
    model_provider: Optional[str] = None
    model: Optional[str] = None

    def __post_init__(self) -> None:
        require_aware_datetime(self.started_at, "started_at")
        _require_nonempty(self.argus_version, "argus_version")
        _require_nonempty(self.adapter_type, "adapter_type")
        _require_nonempty(self.environment_type, "environment_type")
        _require_nonempty(self.evidence_profile, "evidence_profile")
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
        _require_nonempty(self.kind, "kind")


@dataclass(frozen=True)
class StepAttemptRecord:
    step_attempt_id: StepAttemptId
    step_id: StepId
    attempt: int
    status: StepAttemptStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    retry_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")
        require_aware_datetime(self.started_at, "started_at")
        if self.ended_at is not None:
            require_aware_datetime(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("ended_at cannot precede started_at")


@dataclass(frozen=True)
class ActionRecord:
    action_id: ActionId
    step_id: StepId
    step_attempt_id: StepAttemptId
    action_type: str
    parameters: Mapping[str, EvidenceValue] = field(default_factory=dict)
    operation_id: Optional[ActionOperationId] = None

    def __post_init__(self) -> None:
        _require_nonempty(self.action_type, "action_type")
        if not all(isinstance(key, str) and isinstance(value, EvidenceValue) for key, value in self.parameters.items()):
            raise ValueError("action parameters must map strings to EvidenceValue")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class ObservationRecord:
    observation_id: ObservationId
    step_attempt_id: StepAttemptId
    source: str
    captured_at: datetime
    capture_policy: str
    facts: Mapping[str, EvidenceValue]

    def __post_init__(self) -> None:
        require_aware_datetime(self.captured_at, "captured_at")
        _require_nonempty(self.source, "source")
        _require_nonempty(self.capture_policy, "capture_policy")
        if not all(isinstance(key, str) and isinstance(value, EvidenceValue) for key, value in self.facts.items()):
            raise ValueError("observation facts must map strings to EvidenceValue")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


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

    def __post_init__(self) -> None:
        _require_nonempty(self.kind, "kind")
        _require_nonempty(self.method, "method")


@dataclass(frozen=True)
class FindingRecord:
    finding_id: FindingId
    title: EvidenceValue
    description: EvidenceValue
    evidence_refs: tuple[str, ...] = ()
    classification_source: str = "model"

    def __post_init__(self) -> None:
        _require_nonempty(self.classification_source, "classification_source")
        for ref in self.evidence_refs:
            _require_nonempty(ref, "evidence_ref")


def validate_artifact_path(path: str) -> str:
    _require_nonempty(path, "artifact path")
    if "\\" in path or "%" in path or ":" in path:
        raise ValueError("artifact path must use canonical unencoded POSIX-relative syntax")
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        raise ValueError("artifact path must be relative")
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
    protection_state: EvidenceDisposition

    def __post_init__(self) -> None:
        _require_nonempty(self.kind, "kind")
        _require_nonempty(self.sensitivity, "sensitivity")
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

    @property
    def identity_key(self) -> tuple[str, str, Optional[str], Optional[str], Optional[str]]:
        return (
            self.source_system,
            self.requirement_id,
            self.version,
            self.source_revision,
            self.commitment.value if self.commitment else None,
        )


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    method: Optional[str] = None
    actor: Optional[str] = None
    binding_ref: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status is VerificationStatus.VERIFIED:
            _require_nonempty(self.method or "", "verification method")
            _require_nonempty(self.actor or "", "verified actor")
            _require_nonempty(self.binding_ref or "", "binding_ref")
        for value, name in ((self.method, "verification method"), (self.actor, "actor"), (self.binding_ref, "binding_ref")):
            if value is not None:
                _require_nonempty(value, name)


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

    def __post_init__(self) -> None:
        if self.revision < 1 or self.evidence_revision < 1:
            raise ValueError("finalization/evidence revisions must be >= 1")
        require_aware_datetime(self.finalized_at, "finalized_at")
        _require_nonempty(self.status_policy_version, "status_policy_version")
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
        if self.sequence < 1:
            raise ValueError("event sequence must be >= 1")
        require_aware_datetime(self.occurred_at, "occurred_at")
