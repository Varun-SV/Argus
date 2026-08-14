"""Pre-persistence privacy policy for canonical ATES evidence.

The privacy boundary is deliberately separate from execution objects.  Runtime
values are projected into :class:`EvidenceValue` records before they are handed
to the ATES event store; the original action/observation object is never mutated
for logging or redaction purposes.

PR #20 covers JSON/text evidence only.  Binary screenshots and collected files
remain the responsibility of the protected artifact pipeline.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Protocol, Sequence

from .core import EvidenceValue, JsonValue, to_json_compatible

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

    Implementations must persist the supplied JSON value in a protected store
    and return an opaque reference.  The returned reference itself is validated
    before it is admitted into canonical ATES evidence.
    """

    def put(
        self,
        value: JsonValue,
        *,
        context: EvidenceContext,
        field_name: Optional[str],
    ) -> str:
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


_SECRET_REF_RE = re.compile(
    r"^secret://[A-Za-z0-9._-]{1,64}(?:/[A-Za-z0-9._-]{1,128})+$"
)
_PROTECTED_REF_RE = re.compile(
    r"^protected://[a-z0-9][a-z0-9._-]{0,31}/[A-Za-z0-9._-]{1,160}$"
)

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

# Authored/execution inputs are represented as redacted placeholders by default
# so canonical evidence can retain the fact that a value was present.  Values
# originating from the target/model/log stream are higher risk and are omitted
# entirely unless a protected sink is explicitly configured.
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
    """Convert runtime values into secret-safe :class:`EvidenceValue` records."""

    def __init__(
        self,
        config: Optional[EvidencePrivacyConfig] = None,
        *,
        protected_sink: Optional[ProtectedEvidenceSink] = None,
    ) -> None:
        self.config = config or EvidencePrivacyConfig()
        self.protected_sink = protected_sink

    @classmethod
    def standard(cls) -> "EvidencePrivacyPolicy":
        """Return the conservative default runtime policy."""
        return cls(EvidencePrivacyConfig())

    @property
    def policy_id(self) -> str:
        """Stable identity of the effective privacy decision configuration.

        The standard policy retains the human-readable version verbatim.  Once
        contexts are routed to protected storage, a deterministic suffix binds
        that actual context set so two materially different policies cannot
        silently share run provenance just because a caller reused a base ID.
        """
        contexts = tuple(sorted(context.value for context in self.config.protected_contexts))
        if not contexts:
            return self.config.policy_id
        descriptor = "\x1f".join(contexts).encode("utf-8")
        suffix = hashlib.sha256(descriptor).hexdigest()[:12]
        return f"{self.config.policy_id}.{suffix}"

    def capture(
        self,
        value: object,
        *,
        context: EvidenceContext,
        field_name: Optional[str] = None,
        secret_refs: Sequence[str] = (),
    ) -> EvidenceValue:
        """Classify *value* before ordinary ATES persistence.

        This function never includes the raw value in an exception message.
        ``None`` is safe structural absence.  All other values follow the
        context policy unless the context is explicitly routed to a protected
        sink.
        """
        context = self._context(context)
        refs = self._secret_refs(secret_refs)

        if value is None:
            return EvidenceValue.safe(None)

        if any(self._reference_contains_raw_value(ref, value) for ref in refs):
            raise PrivacyPolicyError("secret_refs must be opaque references")

        if context in self.config.protected_contexts:
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
        """Project one action value without mutating the executable action.

        Before validation every model-controlled value is treated as sensitive.
        After validation, only a small structural vocabulary is admitted as
        ordinary ``safe`` evidence; text/URL/command/menu/report prose remains
        redacted or protected according to policy.  An explicit protected
        ACTION_PARAMETER context always wins over safe-fact promotion.
        """
        if EvidenceContext.ACTION_PARAMETER in self.config.protected_contexts:
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
        # Exception instances are runtime objects, not JSON evidence.  Project
        # them to text before the generic capture/protected-sink path so an
        # ERROR_TEXT-protected deployment receives the actual failure message
        # without ever placing it in ordinary evidence metadata.
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
        sink = self.protected_sink
        if sink is None:
            raise PrivacyPolicyError(
                f"protected evidence sink is required for {context.value}"
            )
        snapshot = self._json_snapshot(value)
        try:
            protected_ref = sink.put(
                snapshot,
                context=context,
                field_name=field_name,
            )
        except Exception as exc:
            raise PrivacyPolicyError(
                f"protected evidence sink failed for {context.value}"
            ) from exc
        if not isinstance(protected_ref, str) or not _PROTECTED_REF_RE.fullmatch(protected_ref):
            raise PrivacyPolicyError("protected evidence sink returned an invalid opaque reference")
        if self._reference_contains_raw_value(protected_ref, snapshot):
            raise PrivacyPolicyError("protected evidence sink returned a non-opaque reference")
        return EvidenceValue.protected(
            protected_ref,
            _REASON_BY_CONTEXT[context],
        )

    @staticmethod
    def _context(value: EvidenceContext) -> EvidenceContext:
        if isinstance(value, EvidenceContext):
            return value
        try:
            return EvidenceContext(value)
        except (TypeError, ValueError) as exc:
            raise PrivacyPolicyError("unsupported evidence privacy context") from exc

    @staticmethod
    def _secret_refs(values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes, bytearray)):
            raise PrivacyPolicyError("secret_refs must be a sequence of opaque references")
        try:
            refs = tuple(values)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise PrivacyPolicyError("secret_refs could not be snapshotted safely") from exc
        for ref in refs:
            if not isinstance(ref, str) or not _SECRET_REF_RE.fullmatch(ref):
                raise PrivacyPolicyError("secret_refs contains an invalid opaque reference")
        return refs

    @staticmethod
    def _json_snapshot(value: object) -> JsonValue:
        # Core's SAFE constructor is reused only as a validation/snapshot
        # primitive here.  The resulting plaintext object is passed solely to
        # the protected sink and is never emitted into ordinary evidence.
        try:
            frozen = EvidenceValue.safe(value).value  # type: ignore[arg-type]
            return to_json_compatible(frozen)
        except (TypeError, ValueError) as exc:
            raise PrivacyPolicyError(
                "protected text/JSON evidence contains an unsupported value"
            ) from exc

    @classmethod
    def _reference_contains_raw_value(cls, reference: str, value: object) -> bool:
        """Return whether a reference copies any JSON scalar or mapping key.

        References are metadata that enters ordinary JSONL, so even very short
        strings, numeric values, booleans, and object keys must not be copied
        into them.  Empty strings and ``None`` carry no identifying token and
        are ignored.
        """
        if isinstance(value, str):
            return bool(value) and value in reference
        if isinstance(value, bool):
            token = "true" if value else "false"
            return token in reference.lower()
        if isinstance(value, (int, float)):
            return str(value) in reference
        if isinstance(value, dict):
            return any(
                cls._reference_contains_raw_value(reference, key)
                or cls._reference_contains_raw_value(reference, child)
                for key, child in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(
                cls._reference_contains_raw_value(reference, child)
                for child in value
            )
        return False

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
                "space",
                "minus",
                "equals",
                "comma",
                "period",
                "slash",
                "semicolon",
                "quote",
                "backquote",
                "bracketleft",
                "bracketright",
                "backslash",
            }
            is_printable = (
                len(key) == 1 and key.isascii() and key.isalnum()
            ) or key in content_keys
            # Bare and shift-only printable keys can enter arbitrary content
            # into the focused control one character at a time.  Keep those
            # redacted.  Ctrl/Alt chords and non-printing/navigation keys are
            # structural interactions and may be admitted after validation.
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
