"""Canonical ATES producer for an OS image build.

Only policy-owned stage names and validated content digests enter canonical
evidence. ISO paths, guest credentials, hypervisor output and exception text
are never passed to the event store.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from argus import __version__
from argus.ates import (
    AtesEventStore, EvidenceContext, EvidencePrivacyPolicy, EvidenceValue,
    EventType, ExecutionKind, ObservationId, ObservationRecord,
    PROVISIONING_STAGES, ProvisioningSource, RunId, RunRecord, RunStatus,
    SourceCommitment, StepAttemptId, StepAttemptRecord, StepAttemptStatus,
    StepId, StepRecord, finalize_revision_one, to_json_compatible,
    verify_finalized_run,
)
from argus.provisioning.model import EnvironmentDefinition, ProvisioningError
from argus.provisioning.planner import ProvisioningPlan


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AtesProvisioningRecorder:
    """Record build stages in the ordinary ATES run/finalization authority.

    Call ``begin_stage`` immediately before each operation and ``complete_stage``
    only after its check succeeds. ``abort`` records an error without persisting
    exception details. ``finish`` returns a bound, verified ATES finalization.
    The publication stage must complete only after the immutable cache rename.
    """

    def __init__(
        self,
        project_dir: str | Path,
        definition: EnvironmentDefinition,
        plan: ProvisioningPlan,
    ) -> None:
        if (plan.environment_id, plan.definition_sha256) != (
            definition.environment_id, definition.definition_sha256
        ):
            raise ProvisioningError("ATES provisioning plan does not match definition")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plan.provider):
            raise ProvisioningError("ATES provisioning provider is invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", plan.output_format):
            raise ProvisioningError("ATES provisioning output format is invalid")

        self.run_id = RunId.new()
        self.privacy = EvidencePrivacyPolicy.standard().snapshot()
        self._next_stage = 0
        self._active: tuple[str, StepAttemptRecord] | None = None
        self._terminal = False
        self._closed = False
        self._step_ids = {stage: StepId.new() for stage in PROVISIONING_STAGES}
        source = ProvisioningSource(
            environment_id=definition.environment_id,
            definition_sha256=definition.definition_sha256,
            source_sha256=definition.source.sha256,
            provider=plan.provider,
            image_format=plan.output_format,
            architecture=definition.machine.architecture,
            machine=asdict(definition.machine),
        )
        plan_identity = {
            "environment_id": plan.environment_id,
            "definition_sha256": plan.definition_sha256,
            "provider": plan.provider,
            "output_format": plan.output_format,
        }
        plan_digest = sha256(json.dumps(
            plan_identity, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")).hexdigest()
        run = RunRecord(
            run_id=self.run_id,
            execution_kind=ExecutionKind.PROVISIONING,
            source=source,
            started_at=_utc_now(),
            argus_version=__version__,
            adapter_type="provisioning",
            environment_type="provisioning",
            evidence_profile=self.privacy.policy_id,
            configuration_commitment=SourceCommitment(
                method="sha256", value=plan_digest,
                canonicalization_profile="argus-provisioning-plan-v1",
            ),
            provider=plan.provider,
        )
        steps = tuple(
            StepRecord(
                step_id=self._step_ids[stage],
                instruction=EvidenceValue.safe(stage),
                kind=stage,
            )
            for stage in PROVISIONING_STAGES
        )
        self._store = AtesEventStore(Path(project_dir), self.run_id)
        try:
            self._store.append(EventType.RUN_STARTED, {
                "run": to_json_compatible(run),
                "steps": [to_json_compatible(step) for step in steps],
            })
            self._store.append(EventType.ENVIRONMENT_PREPARED, {
                "environment_type": "provisioning", "isolated": True,
            })
            self._store.append(EventType.TARGET_LAUNCHED, {
                "target": to_json_compatible(self.privacy.capture(
                    "os_environment_build", context=EvidenceContext.TARGET,
                )),
            })
        except BaseException:
            self._store.close()
            raise

    @property
    def run_dir(self) -> Path:
        return self._store.run_dir

    def begin_stage(self, stage: str) -> None:
        if self._closed or self._terminal or self._active is not None:
            raise ProvisioningError("ATES provisioning stage cannot be started")
        if self._next_stage >= len(PROVISIONING_STAGES) or stage != PROVISIONING_STAGES[self._next_stage]:
            raise ProvisioningError("ATES provisioning stages must follow canonical order")
        attempt = StepAttemptRecord(
            step_attempt_id=StepAttemptId.new(),
            step_id=self._step_ids[stage], attempt=1,
            status=StepAttemptStatus.RUNNING, started_at=_utc_now(),
        )
        self._store.append(EventType.STEP_ATTEMPT_STARTED, {
            "attempt": to_json_compatible(attempt),
        })
        self._active = (stage, attempt)

    def complete_stage(self, stage: str, *, image_sha256: str | None = None) -> None:
        if self._closed or self._terminal or self._active is None or self._active[0] != stage:
            raise ProvisioningError("ATES provisioning stage is not active")
        if stage == "image_hashed":
            if not isinstance(image_sha256, str) or not _SHA256.fullmatch(image_sha256):
                raise ProvisioningError("ATES image_hashed stage requires a SHA-256 digest")
            observation = ObservationRecord(
                observation_id=ObservationId.new(),
                step_attempt_id=self._active[1].step_attempt_id,
                source="provisioning", captured_at=_utc_now(),
                capture_policy="ates-provisioning-v1",
                facts={"image_sha256": EvidenceValue.safe(image_sha256)},
            )
            self._store.append(EventType.OBSERVATION_CAPTURED, {
                "observation": to_json_compatible(observation),
            })
        elif image_sha256 is not None:
            raise ProvisioningError("image digest is accepted only for image_hashed")
        self._complete(StepAttemptStatus.PASSED)
        self._next_stage += 1

    def _complete(self, status: StepAttemptStatus) -> None:
        assert self._active is not None
        stage, started = self._active
        terminal = StepAttemptRecord(
            step_attempt_id=started.step_attempt_id,
            step_id=self._step_ids[stage], attempt=1,
            status=status, started_at=started.started_at, ended_at=_utc_now(),
        )
        self._store.append(EventType.STEP_ATTEMPT_COMPLETED, {
            "attempt": to_json_compatible(terminal),
        })
        self._active = None

    def finish(self):
        """Commit a passed ATES run after all six build stages are complete."""
        if self._closed or self._terminal or self._active is not None:
            raise ProvisioningError("ATES provisioning run cannot be finished")
        if self._next_stage != len(PROVISIONING_STAGES):
            raise ProvisioningError("ATES provisioning lifecycle is incomplete")
        return self._finalize("pass")

    def abort(self, *, cleanup_confirmed: bool = True):
        """Commit an error run; leave it incomplete if cleanup is uncertain."""
        if self._closed or self._terminal:
            raise ProvisioningError("ATES provisioning run is already terminal")
        if self._active is not None:
            self._complete(StepAttemptStatus.ERROR)
        if not cleanup_confirmed:
            self.close()
            return None
        return self._finalize("error")

    def _finalize(self, result: str):
        self._store.append(EventType.TARGET_CLOSED, {})
        self._store.append(EventType.ENVIRONMENT_RELEASED, {})
        self._store.append(EventType.RUN_MARKED_INCOMPLETE, {
            "reason": "runtime.finalization_pending", "execution_result": result,
        })
        self._terminal = True
        finalization = finalize_revision_one(self._store)
        if result == "pass" and finalization.outcome.effective_status is not RunStatus.PASSED:
            raise ProvisioningError("ATES provisioning finalization did not pass")
        return finalization

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._store.close()

    def __enter__(self) -> "AtesProvisioningRecorder":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def verify_provisioning_evidence(
    run_dir: str | Path,
    definition: EnvironmentDefinition,
    plan: ProvisioningPlan,
    image_sha256: str,
) -> None:
    """Bind a published image digest to a passed, manifest-verified ATES run."""
    try:
        root = Path(run_dir).resolve(strict=True)
        result = verify_finalized_run(root)
        if result.outcome.effective_status is not RunStatus.PASSED:
            raise ProvisioningError("provisioning evidence did not pass")
        with AtesEventStore(root.parents[2], result.outcome.run_id) as store:
            events = store.events
        source = events[0].payload["run"]["source"]
        expected = to_json_compatible(ProvisioningSource(
            environment_id=definition.environment_id,
            definition_sha256=definition.definition_sha256,
            source_sha256=definition.source.sha256,
            provider=plan.provider,
            image_format=plan.output_format,
            architecture=definition.machine.architecture,
            machine=asdict(definition.machine),
        ))
        digests = [
            event.payload["observation"]["facts"]["image_sha256"]["value"]
            for event in events
            if event.envelope.event_type is EventType.OBSERVATION_CAPTURED
        ]
        if source != expected or digests != [image_sha256]:
            raise ProvisioningError("provisioning evidence identity does not match image")
    except ProvisioningError:
        raise
    except Exception:
        raise ProvisioningError("published provisioning evidence is unavailable or invalid") from None
