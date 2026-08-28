"""Round-6 canonical event-shape validation for PR #22.

Finalization must not derive a trusted PASS from event kinds whose payloads the
relationship reducer otherwise ignores.  This layer validates sequence
`tombstone` provenance and Finding records before delegating to the already
hardened status/lifecycle reducer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .core import EventType, EvidenceValue, FindingRecord


def _evidence_value(value: object, label: str, impl) -> EvidenceValue:
    if not isinstance(value, Mapping):
        raise impl.FinalizationError(f"{label} is malformed")
    refs = value.get("secret_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise impl.FinalizationError(f"{label} secret_refs are malformed")
    try:
        return EvidenceValue(
            disposition=value.get("disposition"),
            value=value.get("value"),
            reason=value.get("reason"),
            secret_refs=tuple(refs),
            protected_ref=value.get("protected_ref"),
        )
    except (TypeError, ValueError) as exc:
        raise impl.FinalizationError(f"{label} is invalid") from exc


def _validate_finding(value: object, impl) -> None:
    if not isinstance(value, Mapping):
        raise impl.FinalizationError("FINDING_RECORDED finding is malformed")
    refs = value.get("evidence_refs", ())
    if isinstance(refs, (str, bytes, bytearray, Mapping)) or not isinstance(refs, Sequence):
        raise impl.FinalizationError("FINDING_RECORDED evidence_refs are malformed")
    try:
        FindingRecord(
            finding_id=value["finding_id"],
            title=_evidence_value(value["title"], "finding title", impl),
            description=_evidence_value(value["description"], "finding description", impl),
            evidence_refs=tuple(refs),
            classification_source=value.get("classification_source", "model"),
            classification=value.get("classification", "unclassified"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, impl.FinalizationError):
            raise
        raise impl.FinalizationError("FINDING_RECORDED finding is invalid") from exc


def _validate_ignored_event_shapes(events, impl) -> None:
    for event in tuple(events):
        kind = event.envelope.event_type
        if kind is EventType.SEQUENCE_TOMBSTONE:
            reason = event.payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise impl.FinalizationError(
                    "SEQUENCE_TOMBSTONE requires a non-empty reason"
                )
        elif kind is EventType.FINDING_RECORDED:
            _validate_finding(event.payload.get("finding"), impl)


def install(impl) -> None:
    previous_derive = impl._derive

    def derive(events, run_id):
        snapshot = tuple(events)
        _validate_ignored_event_shapes(snapshot, impl)
        return previous_derive(snapshot, run_id)

    impl._derive = derive


__all__ = ["install"]
