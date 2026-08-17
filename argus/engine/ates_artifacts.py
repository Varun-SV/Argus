"""Runtime bridge from observations/collections to protected ATES artifacts.

The bridge is intentionally small: binary policy/storage live in ``argus.ates``
and the existing runtime recorder remains the sole canonical event producer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from argus.ates import (
    ARTIFACT_POLICY_VERSION,
    ArtifactCapturePolicy,
    ArtifactContext,
    ArtifactId,
    ArtifactRecord,
    ArtifactSuppression,
    AtesArtifactRepository,
    EventType,
    to_json_compatible,
)
from argus.engine.ates_runtime import AtesRuntimeError, AtesRuntimeRecorder


@dataclass(frozen=True)
class CapturedRuntimeArtifact:
    artifact_id: str
    retained: bool
    protected: bool


class RuntimeArtifactCapture:
    """Capture binary evidence under the authority of one ATES recorder.

    PR #21 keeps integrated Runner/Roam capture on the fixed
    ``ates-artifact-v1`` profile. Lower-level repository policies may classify
    data differently, but non-standard runtime policy selection remains gated
    until its identity can be bound into Run provenance during finalization.
    """

    def __init__(
        self,
        recorder: AtesRuntimeRecorder,
        policy: Optional[ArtifactCapturePolicy] = None,
    ) -> None:
        if not isinstance(recorder, AtesRuntimeRecorder):
            raise ValueError("runtime artifact capture requires an AtesRuntimeRecorder")
        if policy is not None:
            if type(policy) is not ArtifactCapturePolicy:
                raise AtesRuntimeError(
                    "custom runtime artifact policy implementations are not provenance-bound"
                )
            snapshot = policy.snapshot()
            if snapshot.policy_id != ARTIFACT_POLICY_VERSION or snapshot.sanitizer is not None:
                raise AtesRuntimeError(
                    "non-standard runtime artifact policy is not provenance-bound yet"
                )
            policy = snapshot
        self.recorder = recorder
        self.repository = AtesArtifactRepository(recorder._store, policy)

    @property
    def policy_id(self) -> str:
        return self.repository.policy.policy_id

    def _relationship(self) -> dict[str, object]:
        attempt_id = self.recorder.current_attempt_id
        return {"step_attempt_id": str(attempt_id) if attempt_id is not None else None}

    def _emit_suppression(self, suppression: ArtifactSuppression) -> None:
        self.recorder._append(
            EventType.ARTIFACT_SUPPRESSED,
            {
                "artifact_id": str(suppression.artifact_id),
                "context": suppression.context.value,
                "kind": suppression.kind,
                "capture_policy": suppression.capture_policy,
                "reason": suppression.reason,
                **self._relationship(),
            },
        )

    def _emit_checkpoint(self, record: ArtifactRecord, context: ArtifactContext) -> None:
        self.recorder._append(
            EventType.CHECKPOINT_CAPTURED,
            {
                "artifact": to_json_compatible(record),
                "context": context.value,
                **self._relationship(),
            },
        )

    def _emit_collected(self, record: ArtifactRecord, ordinal: int) -> None:
        self.recorder._append(
            EventType.ARTIFACT_COLLECTED,
            {
                "artifact": to_json_compatible(record),
                "collection_ordinal": int(ordinal),
            },
        )

    def suppress_screenshot(
        self,
        *,
        context: ArtifactContext,
        reason: str = "artifact.capture_unavailable",
    ) -> CapturedRuntimeArtifact:
        if context not in {
            ArtifactContext.FAILURE_SCREENSHOT,
            ArtifactContext.FINDING_SCREENSHOT,
            ArtifactContext.CHECKPOINT_SCREENSHOT,
        }:
            raise ValueError("screenshot suppression requires a screenshot ArtifactContext")
        suppression = ArtifactSuppression(
            artifact_id=ArtifactId.new(),
            context=context,
            kind="screenshot",
            capture_policy=self.policy_id,
            reason=str(reason),
        )
        self._emit_suppression(suppression)
        return CapturedRuntimeArtifact(
            artifact_id=str(suppression.artifact_id), retained=False, protected=False
        )

    def capture_screenshot(
        self,
        data: object,
        *,
        context: ArtifactContext,
    ) -> CapturedRuntimeArtifact:
        if context not in {
            ArtifactContext.FAILURE_SCREENSHOT,
            ArtifactContext.FINDING_SCREENSHOT,
            ArtifactContext.CHECKPOINT_SCREENSHOT,
        }:
            raise ValueError("screenshot capture requires a screenshot ArtifactContext")
        result = self.repository.capture_bytes(
            data,
            context=context,
            kind="screenshot",
            media_type="image/png",
        )
        if result.suppression is not None:
            self._emit_suppression(result.suppression)
            return CapturedRuntimeArtifact(
                artifact_id=str(result.suppression.artifact_id), retained=False, protected=False
            )
        assert result.record is not None
        self._emit_checkpoint(result.record, context)
        return CapturedRuntimeArtifact(
            artifact_id=str(result.record.artifact_id),
            retained=True,
            protected=result.record.protected_ref is not None,
        )

    def collect_declared(self, adapter, guest_paths: Sequence[str]) -> list[dict]:
        """Collect declared Capsule files to opaque protected ATES destinations.

        Guest transfer and final ATES registration validation form one pre-event
        transaction. ``collection_ordinal`` preserves source-spec ordering in
        canonical evidence without persisting the guest filename itself.
        """
        if not guest_paths:
            return []
        collect = getattr(adapter, "collect_artifacts_to_tree", None)
        if not callable(collect):
            raise AtesRuntimeError(
                "execution environment does not support protected ATES artifact collection"
            )
        reservations = self.repository.reserve_protected_collection(len(guest_paths))
        entries = [
            {"path": str(guest_path), "destination": reservation.relative_path}
            for guest_path, reservation in zip(guest_paths, reservations)
        ]
        relatives = [reservation.relative_path for reservation in reservations]

        with self.repository.open_tree(relatives) as tree:
            try:
                transferred = list(collect(entries, tree))
                if len(transferred) != len(reservations):
                    raise AtesRuntimeError(
                        "protected artifact collection returned an unexpected artifact count"
                    )
                records: list[ArtifactRecord] = []
                for reservation, metadata in zip(reservations, transferred):
                    if not isinstance(metadata, Mapping):
                        raise AtesRuntimeError("artifact collection metadata must be a mapping")
                    records.append(
                        self.repository.finalize_reserved(
                            tree,
                            reservation,
                            expected_size=metadata.get("size"),
                            expected_sha256=metadata.get("sha256"),
                        )
                    )
            except BaseException as exc:
                rollback_errors: list[BaseException] = []
                for reservation in reversed(reservations):
                    try:
                        tree.unlink_relative(reservation.relative_path)
                    except FileNotFoundError:
                        pass
                    except BaseException as rollback_exc:
                        rollback_errors.append(rollback_exc)
                if rollback_errors:
                    raise AtesRuntimeError(
                        "protected artifact registration failed and rollback was incomplete"
                    ) from rollback_errors[0]
                raise exc

        # Verified files are now retained. If an event append is ambiguous, do
        # not delete them because the event itself may already be durable.
        for ordinal, record in enumerate(records, 1):
            self._emit_collected(record, ordinal)

        return [
            {
                "artifact_id": str(record.artifact_id),
                "size": record.size_bytes,
                "sha256": record.content_digest.value.removeprefix("sha256:"),
                "protected": True,
            }
            for record in records
        ]


__all__ = ["CapturedRuntimeArtifact", "RuntimeArtifactCapture"]
