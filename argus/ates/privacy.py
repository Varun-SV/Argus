"""Pre-persistence privacy policy for canonical ATES evidence.

The privacy boundary is deliberately separate from execution objects. Runtime
values are projected into :class:`EvidenceValue` records before they are handed
to the ATES event store; the original action/observation object is never mutated
for logging or redaction purposes.

PR #20 covers JSON/text evidence only. Binary screenshots and collected files
remain the responsibility of the protected artifact pipeline.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Protocol, Sequence

from .core import EvidenceValue, JsonValue, freeze_json, to_json_compatible

PRIVACY_POLICY_VERSION = "ates-privacy-v1"


class PrivacyPolicyError(RuntimeError):
    """Raised when evidence cannot be represented under the active policy."""


class EvidenceContext(str, Enum):
    """Origin/semantic class used to make a pre-persistence privacy decision."""

    STEP_INSTRUCTION = "step_instruction"
    TARGET = "target"
    RETRY_REASON = "retry_reason"
    ACTION_PARAMETER = "action_parameter"
    OBSERVATION_WINDOW_TITLE = "observation_window_title"
    OBSERVATION_UI_TREE = "observation_ui_tree"
    OBSERVATION_DIALOGS = "observation_dialogs"
    OBSERVATION_ERROR = "observation_error"
    OBSERVATION_STDOUT = "observation_stdout"
    OBSERVATION_STDERR = "observation_stderr"
    OBSERVATION_URL = "observation_url"
    ASSERTION_EXPECTED = "assertion_expected"
    ASSERTION_ACTUAL = "assertion_actual"
    FINDING_TITLE = "finding_title"
    FINDING_DESCRIPTION = "finding_description"
    ERROR_TEXT = "error_text"
    LOG_EXCERPT = "log_excerpt"
    OPERATOR_ANNOTATION = "operator_annotation"


class ProtectedEvidenceSink(Protocol):
    """Authorized sink for evidence that may not appear in ordinary JSONL.

    Argus creates an opaque protected reference *before* the plaintext value is
    handed to the sink. Implementations must persist ``value`` under the supplied
    ``protected_ref``. The sink never chooses canonical evidence metadata, so a
    payload-derived or mutating sink cannot copy plaintext back into JSONL.
    """

    def put(
        self,
        value: JsonValue,
        *,
        context: EvidenceContext,
        field_name: Optional[str],
        protected_ref: str,
    ) -> None:
        ...


@dataclass(frozen=True)
class EvidencePrivacyConfig:
    """Versioned privacy configuration for one ATES producer."""

    policy_id: str = PRIVACY_POLICY_VERSION
    protected_contexts: FrozenSet[EvidenceContext] = frozenset()

    def __post_init__(self) -> None:
        policy_id = str(self.policy_id or "").strip()
        if not policy_id or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", policy_id):
            raise ValueError("privacy policy_id must be a safe non-empty identifier")
        object.__setattr__(self, "policy_id", policy_id)

        try:
            contexts = frozenset(
                item if isinstance(item, EvidenceContext) else EvidenceContext(item)
                for item in self.protected_contexts
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("protected_contexts contains an unsupported evidence context") from exc
        object.__setattr__(self, "protected_contexts", contexts)


_SECRET_REF_RE = re.compile(r"^secret://ates/[0-9a-f]{32}$")
_PROTECTED_REF_RE = re.compile(r"^protected://ates/[0-9a-f]{32}$")

_REASON_BY_CONTEXT = {
    EvidenceContext.STEP_INSTRUCTION: "privacy.authored_text",
    EvidenceContext.TARGET: "privacy.target_value",
    EvidenceContext.RETRY_REASON: "privacy.retry_reason",
    EvidenceContext.ACTION_PARAMETER: "privacy.action_value",
    EvidenceContext.OBSERVATION_WINDOW_TITLE: "privacy.target_generated",
    EvidenceContext.OBSERVATION_UI_TREE: "privacy.target_generated",
    EvidenceContext.OBSERVATION_DIALOGS: "privacy.target_generated",
    EvidenceContext.OBSERVATION_ERROR: "privacy.error_text",
    EvidenceContext.OBSERVATION_STDOUT: "privacy.target_generated",
    EvidenceContext.OBSERVATION_STDERR: "privacy.target_generated",
    EvidenceContext.OBSERVATION_URL: "privacy.target_generated",
    EvidenceContext.ASSERTION_EXPECTED: "privacy.assertion_value",
    EvidenceContext.ASSERTION_ACTUAL: "privacy.target_generated",
    EvidenceContext.FINDING_TITLE: "privacy.finding_text",
    EvidenceContext.FINDING_DESCRIPTION: "privacy.finding_text",
    EvidenceContext.ERROR_TEXT: "privacy.error_text",
    EvidenceContext.LOG_EXCERPT: "privacy.log_excerpt",
    EvidenceContext.OPERATOR_ANNOTATION: "privacy.operator_annotation",
}

_REDACT_BY_DEFAULT = frozenset(
    {
        EvidenceContext.STEP_INSTRUCTION,
        EvidenceContext.TARGET,
        EvidenceContext.RETRY_REASON,
        EvidenceContext.ACTION_PARAMETER,
        EvidenceContext.ASSERTION_EXPECTED,
        EvidenceContext.OPERATOR_ANNOTATION,
    }
)


class EvidencePrivacyPolicy:
    """Convert runtime values into secret-safe :class:`EvidenceValue` records.

    Configuration and protected-sink bindings are read-only after construction.
    ``snapshot()`` creates a distinct policy instance for one run so later state
    on a caller-owned policy cannot change committed run provenance or storage
    routing.
    """

    def __init__(
        self,
        config: Optional[EvidencePrivacyConfig] = None,
        *,
        protected_sink: Optional[ProtectedEvidenceSink] = None,
    ) -> None:
        self._config = config or EvidencePrivacyConfig()
        self._protected_sink = protected_sink
        self._issued_secret_refs: set[str] = set()
        self._issued_protected_refs: set[str] = set()

    @classmethod
    def standard(cls) -> "EvidencePrivacyPolicy":
        return cls(EvidencePrivacyConfig())

    @property
    def config(self) -> EvidencePrivacyConfig:
        return self._config

    @property
    def protected_sink(self) -> Optional[ProtectedEvidenceSink]:
        return self._protected_sink

    def snapshot(self) -> "EvidencePrivacyPolicy":
        """Return an isolated policy snapshot suitable for one ATES run."""
        snap = EvidencePrivacyPolicy(
            EvidencePrivacyConfig(
                policy_id=self._config.policy_id,
                protected_contexts=frozenset(self._config.protected_contexts),
            ),
            protected_sink=self._protected_sink,
        )
        # Secret aliases may be issued before a run is constructed. Preserve
        # only that validation provenance; protected references are always
        # generated afresh inside the run-local snapshot.
        snap._issued_secret_refs.update(self._issued_secret_refs)
        return snap

    @property
    def policy_id(self) -> str:
        contexts = tuple(sorted(context.value for context in self._config.protected_contexts))
        if not contexts:
            return self._config.policy_id
        descriptor = "\x1f".join(contexts).encode("utf-8")
        suffix = hashlib.sha256(descriptor).hexdigest()[:12]
        return f"{self._config.policy_id}.{suffix}"

    def issue_secret_ref(self) -> str:
        """Issue a payload-independent opaque alias for external secret storage."""
        while True:
            ref = f"secret://ates/{secrets.token_hex(16)}"
            if ref not in self._issued_secret_refs:
                self._issued_secret_refs.add(ref)
                return ref

    def capture(
        self,
        value: object,
        *,
        context: EvidenceContext,
        field_name: Optional[str] = None,
        secret_refs: Sequence[str] = (),
    ) -> EvidenceValue:
        context = self._context(context)
        return self._capture_classified(
            value,
            context=context,
            field_name=field_name,
            secret_refs=secret_refs,
        )

    def _capture_classified(
        self,
        value: object,
        *,
        context: EvidenceContext,
        field_name: Optional[str],
        secret_refs: Sequence[str],
    ) -> EvidenceValue:
        refs = self._secret_refs(secret_refs)
        if value is None:
            return EvidenceValue.safe(None)
        if refs:
            self._freeze_json_snapshot(value)
        if context in self._config.protected_contexts:
            return self._protect(value, context=context, field_name=field_name)
        reason = _REASON_BY_CONTEXT[context]
        if context in _REDACT_BY_DEFAULT:
            return EvidenceValue.redacted(reason, secret_refs=refs)
        return EvidenceValue.suppressed(reason)

    def action_parameter(
        self,
        action_type: str,
        parameter: str,
        value: object,
        *,
        validated: bool,
        secret_refs: Sequence[str] = (),
    ) -> EvidenceValue:
        if EvidenceContext.ACTION_PARAMETER in self._config.protected_contexts:
            return self.capture(
                value,
                context=EvidenceContext.ACTION_PARAMETER,
                field_name=parameter,
                secret_refs=secret_refs,
            )
        if not validated:
            return self.capture(
                value,
                context=EvidenceContext.ACTION_PARAMETER,
                field_name=parameter,
                secret_refs=secret_refs,
            )
        kind = str(action_type or "").strip().lower()
        name = str(parameter or "").strip()
        structural = self._safe_action_scalar(kind, name, value)
        if structural is not _NOT_STRUCTURAL:
            return EvidenceValue.safe(structural)  # type: ignore[arg-type]
        return self.capture(
            value,
            context=EvidenceContext.ACTION_PARAMETER,
            field_name=name,
            secret_refs=secret_refs,
        )

    def observation_value(self, field_name: str, value: object) -> EvidenceValue:
        context = {
            "window_title": EvidenceContext.OBSERVATION_WINDOW_TITLE,
            "ui_tree": EvidenceContext.OBSERVATION_UI_TREE,
            "dialogs": EvidenceContext.OBSERVATION_DIALOGS,
            "error": EvidenceContext.OBSERVATION_ERROR,
            "stdout": EvidenceContext.OBSERVATION_STDOUT,
            "stderr": EvidenceContext.OBSERVATION_STDERR,
            "url": EvidenceContext.OBSERVATION_URL,
        }.get(field_name)
        if context is None:
            raise PrivacyPolicyError("unsupported observation privacy field")
        return self.capture(value, context=context, field_name=field_name)

    def assertion_expected(self, value: object) -> EvidenceValue:
        return self.capture(value, context=EvidenceContext.ASSERTION_EXPECTED)

    def assertion_actual(self, value: object) -> EvidenceValue:
        return self.capture(value, context=EvidenceContext.ASSERTION_ACTUAL)

    def finding_title(self, value: object) -> EvidenceValue:
        return self.capture(value, context=EvidenceContext.FINDING_TITLE)

    def finding_description(self, value: object) -> EvidenceValue:
        return self.capture(value, context=EvidenceContext.FINDING_DESCRIPTION)

    def error_text(self, value: object) -> EvidenceValue:
        if isinstance(value, BaseException):
            value = str(value)
        return self.capture(value, context=EvidenceContext.ERROR_TEXT)

    def _protect(
        self,
        value: object,
        *,
        context: EvidenceContext,
        field_name: Optional[str],
    ) -> EvidenceValue:
        sink = self._protected_sink
        if sink is None:
            raise PrivacyPolicyError(
                f"protected evidence sink is required for {context.value}"
            )
        frozen_snapshot = self._freeze_json_snapshot(value)
        sink_snapshot = to_json_compatible(frozen_snapshot)
        protected_ref = self._issue_protected_ref()
        try:
            sink.put(
                sink_snapshot,
                context=context,
                field_name=field_name,
                protected_ref=protected_ref,
            )
        except Exception as exc:
            raise PrivacyPolicyError(
                f"protected evidence sink failed for {context.value}"
            ) from exc
        return EvidenceValue.protected(protected_ref, _REASON_BY_CONTEXT[context])

    def _issue_protected_ref(self) -> str:
        while True:
            ref = f"protected://ates/{secrets.token_hex(16)}"
            if ref not in self._issued_protected_refs:
                if not _PROTECTED_REF_RE.fullmatch(ref):
                    raise PrivacyPolicyError("generated protected reference is invalid")
                self._issued_protected_refs.add(ref)
                return ref

    @staticmethod
    def _context(value: EvidenceContext) -> EvidenceContext:
        if isinstance(value, EvidenceContext):
            return value
        try:
            return EvidenceContext(value)
        except (TypeError, ValueError) as exc:
            raise PrivacyPolicyError("unsupported evidence privacy context") from exc

    def _secret_refs(self, values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes, bytearray)):
            raise PrivacyPolicyError("secret_refs must be a sequence of opaque references")
        try:
            refs = tuple(values)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise PrivacyPolicyError("secret_refs could not be snapshotted safely") from exc
        for ref in refs:
            if (
                not isinstance(ref, str)
                or not _SECRET_REF_RE.fullmatch(ref)
                or ref not in self._issued_secret_refs
            ):
                raise PrivacyPolicyError(
                    "secret_refs must be policy-issued opaque references"
                )
        return refs

    @staticmethod
    def _freeze_json_snapshot(value: object) -> JsonValue:
        try:
            return freeze_json(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise PrivacyPolicyError(
                "protected/redacted text/JSON evidence contains an unsupported value"
            ) from exc

    @staticmethod
    def _safe_action_scalar(action_type: str, parameter: str, value: object) -> object:
        if parameter in {"element_id", "x", "y"} and action_type in {
            "click",
            "double_click",
            "right_click",
            "type",
        }:
            if isinstance(value, bool) or not isinstance(value, int):
                return _NOT_STRUCTURAL
            return value
        if action_type == "scroll" and parameter == "direction":
            return value if value in {"up", "down"} else _NOT_STRUCTURAL
        if action_type == "scroll" and parameter == "amount":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                return _NOT_STRUCTURAL
            return value
        if action_type == "wait" and parameter == "seconds":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return _NOT_STRUCTURAL
            number = float(value)
            if not math.isfinite(number) or not 0 <= number <= 30:
                return _NOT_STRUCTURAL
            return value
        if action_type == "done" and parameter == "success":
            return value if isinstance(value, bool) else _NOT_STRUCTURAL
        if action_type == "key" and parameter == "keys":
            if not isinstance(value, str):
                return _NOT_STRUCTURAL
            try:
                from argus.actions import ActionValidationError, canonicalize_key_chord
                canonical = canonicalize_key_chord(value)
            except (ActionValidationError, TypeError, ValueError):
                return _NOT_STRUCTURAL
            if canonical != value:
                return _NOT_STRUCTURAL
            parts = canonical.split("+")
            modifiers = set(parts[:-1])
            key = parts[-1]
            content_keys = {
                "space", "minus", "equals", "comma", "period", "slash",
                "semicolon", "quote", "backquote", "bracketleft",
                "bracketright", "backslash",
            }
            is_printable = (
                len(key) == 1 and key.isascii() and key.isalnum()
            ) or key in content_keys
            if is_printable and not (modifiers & {"ctrl", "alt"}):
                return _NOT_STRUCTURAL
            return value
        if action_type == "report_bug" and parameter == "severity":
            if value in {"low", "medium", "high", "critical"}:
                return value
            return _NOT_STRUCTURAL
        return _NOT_STRUCTURAL


class _NotStructural:
    __slots__ = ()


_NOT_STRUCTURAL = _NotStructural()


__all__ = [
    "PRIVACY_POLICY_VERSION",
    "EvidenceContext",
    "EvidencePrivacyConfig",
    "EvidencePrivacyPolicy",
    "PrivacyPolicyError",
    "ProtectedEvidenceSink",
]
