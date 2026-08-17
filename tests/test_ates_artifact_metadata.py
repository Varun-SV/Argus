from __future__ import annotations

import hashlib

import pytest

from argus.ates import (
    ArtifactCaptureError,
    ArtifactContext,
    ArtifactId,
    ArtifactReservation,
    ArtifactSuppression,
    AtesArtifactRepository,
    AtesEventStore,
    RunId,
)


def test_suppression_reason_rejects_free_form_plaintext():
    with pytest.raises(ValueError, match="safe reason code"):
        ArtifactSuppression(
            artifact_id=ArtifactId.new(),
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            capture_policy="ates-artifact-v1",
            reason="customer password was visible",
        )


def test_forged_reservation_cannot_register_an_existing_file(tmp_path):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    try:
        issued = repository.reserve_protected_collection(1)[0]
        forged = ArtifactReservation(
            artifact_id=issued.artifact_id,
            context=issued.context,
            kind=issued.kind,
            media_type=issued.media_type,
            relative_path=issued.relative_path,
            protected_ref="protected://ates/" + "0" * 32,
        )
        with repository.open_tree((issued.relative_path,)) as tree:
            handle, temp_name, _ = tree.open_temp_file(issued.relative_path)
            with handle:
                handle.write(b"private")
                handle.flush()
            tree.commit_temp(issued.relative_path, temp_name)
            with pytest.raises(ArtifactCaptureError, match="not issued by this repository"):
                repository.finalize_reserved(tree, forged)
            record = repository.finalize_reserved(
                tree,
                issued,
                expected_size=7,
                expected_sha256=hashlib.sha256(b"private").hexdigest(),
            )
        assert record.protected_ref == issued.protected_ref
    finally:
        store.close()


def test_reservation_can_only_be_finalized_once(tmp_path):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    try:
        reservation = repository.reserve_protected_collection(1)[0]
        with repository.open_tree((reservation.relative_path,)) as tree:
            handle, temp_name, _ = tree.open_temp_file(reservation.relative_path)
            with handle:
                handle.write(b"payload")
                handle.flush()
            tree.commit_temp(reservation.relative_path, temp_name)
            repository.finalize_reserved(tree, reservation)
            with pytest.raises(ArtifactCaptureError, match="already finalized"):
                repository.finalize_reserved(tree, reservation)
    finally:
        store.close()
