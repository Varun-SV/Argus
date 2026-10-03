"""Canonical ATES evidence for provisioned OS images."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from argus.ates import (
    AtesEventStore, EventType, ExecutionKind, RunStatus,
    FinalizationError, render_reports, to_json_compatible, verify_finalized_run,
)
from argus.ates.evidence_validation import derive_evidence_state
from argus.provisioning.evidence import (
    AtesProvisioningRecorder, verify_provisioning_evidence,
)
from argus.provisioning.model import (
    EnvironmentDefinition, InstallationMediaSource, InstallationSpec, MachineSpec,
    ProvisioningError,
)
from argus.provisioning.planner import ProvisioningPlan


def _inputs(tmp_path: Path):
    definition = EnvironmentDefinition(
        name="private-operator-name",
        source=InstallationMediaSource(
            path="/private/operator/secret-media.iso", sha256="a" * 64,
        ),
        machine=MachineSpec(
            cpu_count=4, memory_mb=8192, firmware="uefi",
            secure_boot=True, tpm_version="2.0", disk_bus="scsi",
            network_mode="host_only",
        ),
        installation=InstallationSpec(
            unattended=False, credential_ref="secret://operator/bootstrap-token",
        ),
    )
    cache = tmp_path / "cache" / definition.environment_id / "hyperv" / "vhdx"
    plan = ProvisioningPlan(
        environment_id=definition.environment_id,
        definition_sha256=definition.definition_sha256,
        provider="hyperv", output_format="vhdx", cache_dir=cache,
        image_path=cache / "base.vhdx", manifest_path=cache / "manifest.json",
    )
    return definition, plan


def test_provisioning_evidence_finalizes_and_hides_locators_and_secrets(tmp_path):
    definition, plan = _inputs(tmp_path)
    with AtesProvisioningRecorder(tmp_path, definition, plan) as recorder:
        run_dir = recorder.run_dir
        run_id = recorder.run_id
        for stage in (
            "media_verified", "provider_selected", "installation",
            "baseline_validated", "image_hashed", "publication",
        ):
            recorder.begin_stage(stage)
            recorder.complete_stage(
                stage, image_sha256="b" * 64 if stage == "image_hashed" else None,
            )
        result = recorder.finish()
        assert result.outcome.effective_status is RunStatus.PASSED
    verified = verify_finalized_run(run_dir)
    assert verified.outcome.effective_status is RunStatus.PASSED
    verify_provisioning_evidence(run_dir, definition, plan, "b" * 64)
    with pytest.raises(ProvisioningError, match="identity"):
        verify_provisioning_evidence(run_dir, definition, plan, "c" * 64)
    changed = replace(definition, machine=replace(definition.machine, cpu_count=8))
    with pytest.raises(ProvisioningError, match="identity"):
        verify_provisioning_evidence(run_dir, changed, plan, "b" * 64)
    with AtesEventStore(tmp_path, run_id) as store:
        events = store.events
    assert events[0].payload["run"]["execution_kind"] == ExecutionKind.PROVISIONING.value
    assert events[0].payload["run"]["source"]["machine"]["secure_boot"] is True
    assert any(e.envelope.event_type is EventType.OBSERVATION_CAPTURED for e in events)
    raw = (run_dir / "evidence.jsonl").read_text(encoding="utf-8")
    assert "secret-media.iso" not in raw
    assert "private-operator-name" not in raw
    assert "secret://operator/bootstrap-token" not in raw
    assert '"value":"' + "b" * 64 + '"' in raw
    report = render_reports(run_dir)
    assert report.json_path.is_file()
    assert "secret-media.iso" not in report.json_path.read_text(encoding="utf-8")


def test_provisioning_error_finalizes_without_exception_text(tmp_path):
    definition, plan = _inputs(tmp_path)
    with AtesProvisioningRecorder(tmp_path, definition, plan) as recorder:
        run_dir = recorder.run_dir
        recorder.begin_stage("media_verified")
        result = recorder.abort()
        assert result.outcome.effective_status is RunStatus.ERROR
    assert verify_finalized_run(run_dir).outcome.effective_status is RunStatus.ERROR


def test_uncertain_cleanup_keeps_provisioning_run_incomplete(tmp_path):
    definition, plan = _inputs(tmp_path)
    with AtesProvisioningRecorder(tmp_path, definition, plan) as recorder:
        run_dir = recorder.run_dir
        recorder.begin_stage("media_verified")
        assert recorder.abort(cleanup_confirmed=False) is None
    with pytest.raises(FinalizationError):
        verify_finalized_run(run_dir)
    report = render_reports(run_dir)
    assert report.json_path.is_file()


def test_provisioning_stage_order_and_image_digest_are_required(tmp_path):
    definition, plan = _inputs(tmp_path)
    with AtesProvisioningRecorder(tmp_path, definition, plan) as recorder:
        try:
            recorder.begin_stage("publication")
            assert False, "out-of-order stage was accepted"
        except ProvisioningError:
            pass
        recorder.begin_stage("media_verified")
        recorder.complete_stage("media_verified")
        recorder.begin_stage("provider_selected")
        recorder.complete_stage("provider_selected")
        recorder.begin_stage("installation")
        recorder.complete_stage("installation")
        recorder.begin_stage("baseline_validated")
        recorder.complete_stage("baseline_validated")
        recorder.begin_stage("image_hashed")
        try:
            recorder.complete_stage("image_hashed", image_sha256="invalid")
            assert False, "invalid digest was accepted"
        except ProvisioningError:
            pass
        recorder.abort()


def test_canonical_verifier_rejects_forged_image_digest(tmp_path):
    definition, plan = _inputs(tmp_path)
    with AtesProvisioningRecorder(tmp_path, definition, plan) as recorder:
        for stage in (
            "media_verified", "provider_selected", "installation",
            "baseline_validated", "image_hashed", "publication",
        ):
            recorder.begin_stage(stage)
            recorder.complete_stage(
                stage, image_sha256="b" * 64 if stage == "image_hashed" else None,
            )
        recorder.finish()
        events = list(recorder._store.events[:-1])
        for index, event in enumerate(events):
            if event.envelope.event_type is EventType.OBSERVATION_CAPTURED:
                payload = to_json_compatible(event.payload)
                payload["observation"]["facts"]["image_sha256"]["value"] = "unsafe-path"
                events[index] = replace(event, payload=payload)
                break
        else:
            assert False, "image hash observation was missing"
        with pytest.raises(FinalizationError, match="image digest"):
            derive_evidence_state(events, recorder.run_id)
