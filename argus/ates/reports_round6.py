"""Round-6 retained-artifact report provenance for PR #22."""
from __future__ import annotations

from collections.abc import Mapping

from . import reports_runtime as _runtime
from .core import EventType


def install() -> None:
    previous_model = _runtime._model

    def model(root, approval_key_resolver):
        rendered = previous_model(root, approval_key_resolver)
        _result, events = _runtime._verified_events(root)
        checkpoint_by_sequence: dict[int, Mapping[str, object]] = {}
        for event in events:
            if event.envelope.event_type is EventType.CHECKPOINT_CAPTURED:
                checkpoint_by_sequence[event.sequence] = event.payload

        artifacts = rendered.get("artifacts")
        if isinstance(artifacts, list):
            for item in artifacts:
                if not isinstance(item, dict) or "record" not in item:
                    continue
                sequence = item.get("sequence")
                if not isinstance(sequence, int):
                    continue
                payload = checkpoint_by_sequence.get(sequence)
                if payload is None:
                    continue
                item["context"] = payload.get("context")
                item["finding_id"] = payload.get("finding_id")
                item["step_attempt_id"] = payload.get("step_attempt_id")
        return rendered

    _runtime._model = model


__all__ = ["install"]
