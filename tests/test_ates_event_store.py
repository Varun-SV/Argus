from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from argus.ates import ATES_VERSION, EventEnvelope, EventId, EventType, RunId
from argus.ates.core import to_json_compatible
from argus.ates.store import (
    AtesAppendError,
    AtesEventConflict,
    AtesEventStore,
    AtesStoreBusy,
    AtesStoreCorruption,
    AtesStoreError,
    StoredEvent,
)
import argus.ates.store as store_module


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _event(
    run_id: RunId,
    sequence: int,
    *,
    event_id: str | None = None,
    event_type: EventType = EventType.RUN_STARTED,
    payload: dict | None = None,
) -> StoredEvent:
    return StoredEvent(
        EventEnvelope(
            ates_version=ATES_VERSION,
            run_id=run_id,
            event_id=EventId(event_id or f"EVT-event-{sequence}"),
            sequence=sequence,
            event_type=event_type,
            occurred_at=NOW,
        ),
        payload=payload or {},
    )


def _run_path(project: Path, run_id: RunId) -> Path:
    return project / ".argus" / "runs" / str(run_id)


def test_append_persists_canonical_gap_free_jsonl(tmp_path: Path):
    run_id = RunId("RUN-canonical")
    with AtesEventStore(tmp_path, run_id) as store:
        first = store.append(
            EventType.RUN_STARTED,
            {"z": 2, "a": "héllo"},
            occurred_at=NOW,
            event_id="EVT-first",
        )
        second = store.append(
            EventType.ENVIRONMENT_PREPARED,
            {"ok": True},
            occurred_at=NOW,
            event_id="EVT-second",
        )
        assert first.sequence == 1
        assert second.sequence == 2
        assert store.next_sequence == 3

    raw = (_run_path(tmp_path, run_id) / "evidence.jsonl").read_bytes()
    assert raw == first.canonical_line() + second.canonical_line()
    assert raw.endswith(b"\n")
    assert b'"a":"h\xc3\xa9llo"' in raw


def test_reopen_recovers_sequence_and_continues(tmp_path: Path):
    run_id = RunId("RUN-reopen")
    with AtesEventStore(tmp_path, run_id) as store:
        first = store.append(EventType.RUN_STARTED, occurred_at=NOW)

    with AtesEventStore(tmp_path, run_id) as reopened:
        assert reopened.events == (first,)
        assert reopened.next_sequence == 2
        second = reopened.append(EventType.TARGET_LAUNCHED, occurred_at=NOW)
        assert second.sequence == 2


def test_identical_event_replay_is_idempotent(tmp_path: Path):
    run_id = RunId("RUN-idempotent")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(
            EventType.RUN_STARTED,
            {"source": "test"},
            occurred_at=NOW,
            event_id="EVT-stable",
        )
        before = store.path.read_bytes()
        replay = store.append_event(event)
        assert replay == event
        assert store.path.read_bytes() == before
        assert store.next_sequence == 2


def test_same_event_id_with_different_content_is_conflict(tmp_path: Path):
    run_id = RunId("RUN-event-conflict")
    with AtesEventStore(tmp_path, run_id) as store:
        store.append(
            EventType.RUN_STARTED,
            {"value": 1},
            occurred_at=NOW,
            event_id="EVT-same",
        )
        conflicting = _event(
            run_id,
            2,
            event_id="EVT-same",
            payload={"value": 2},
        )
        with pytest.raises(AtesEventConflict, match="event_id"):
            store.append_event(conflicting)


def test_same_sequence_with_different_event_is_conflict(tmp_path: Path):
    run_id = RunId("RUN-sequence-conflict")
    with AtesEventStore(tmp_path, run_id) as store:
        store.append(
            EventType.RUN_STARTED,
            occurred_at=NOW,
            event_id="EVT-original",
        )
        conflicting = _event(run_id, 1, event_id="EVT-other")
        with pytest.raises(AtesEventConflict, match="sequence"):
            store.append_event(conflicting)


def test_gap_is_rejected_without_reserving_sequence(tmp_path: Path):
    run_id = RunId("RUN-gap")
    with AtesEventStore(tmp_path, run_id) as store:
        with pytest.raises(AtesEventConflict, match="expected 1, got 2"):
            store.append_event(_event(run_id, 2))
        assert store.next_sequence == 1
        assert store.path.read_bytes() == b""


def test_event_for_another_run_is_rejected(tmp_path: Path):
    with AtesEventStore(tmp_path, RunId("RUN-owner")) as store:
        with pytest.raises(AtesEventConflict, match="run_id"):
            store.append_event(_event(RunId("RUN-other"), 1))


def test_payload_is_snapshotted_before_persistence(tmp_path: Path):
    run_id = RunId("RUN-snapshot")
    payload = {"nested": {"items": [1, 2]}}
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(
            EventType.RUN_STARTED,
            payload,
            occurred_at=NOW,
        )
        payload["nested"]["items"].append(3)
        assert to_json_compatible(event.payload) == {"nested": {"items": [1, 2]}}


def test_payload_must_be_an_object(tmp_path: Path):
    run_id = RunId("RUN-object-payload")
    with AtesEventStore(tmp_path, run_id) as store:
        with pytest.raises(ValueError, match="payload must be a JSON object"):
            store.append(EventType.RUN_STARTED, ["not", "an", "object"])  # type: ignore[arg-type]


def test_unterminated_tail_fails_closed_by_default(tmp_path: Path):
    run_id = RunId("RUN-tail")
    with AtesEventStore(tmp_path, run_id) as store:
        committed = store.append(EventType.RUN_STARTED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"partial":')

    with pytest.raises(AtesStoreCorruption, match="unterminated trailing record"):
        AtesEventStore(tmp_path, run_id)

    with AtesEventStore(
        tmp_path, run_id, repair_trailing_partial=True
    ) as repaired:
        assert repaired.events == (committed,)
        assert repaired.path.read_bytes() == committed.canonical_line()
        assert repaired.next_sequence == 2


def test_repair_never_skips_a_malformed_complete_line(tmp_path: Path):
    run_id = RunId("RUN-complete-corrupt")
    with AtesEventStore(tmp_path, run_id) as store:
        store.append(EventType.RUN_STARTED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    with path.open("ab") as handle:
        handle.write(b"not-json\n")

    with pytest.raises(AtesStoreCorruption, match="strict JSON"):
        AtesEventStore(tmp_path, run_id, repair_trailing_partial=True)


def test_reopen_rejects_internal_sequence_gap(tmp_path: Path):
    run_id = RunId("RUN-corrupt-gap")
    with AtesEventStore(tmp_path, run_id) as store:
        first = store.append(EventType.RUN_STARTED, occurred_at=NOW)
        second = store.append(EventType.TARGET_LAUNCHED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    doc = json.loads(second.canonical_line())
    doc["sequence"] = 3
    bad_second = (
        json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )
    path.write_bytes(first.canonical_line() + bad_second)

    with pytest.raises(AtesStoreCorruption, match="sequence gap/conflict"):
        AtesEventStore(tmp_path, run_id)


def test_reopen_rejects_duplicate_json_keys(tmp_path: Path):
    run_id = RunId("RUN-duplicate-key")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(EventType.RUN_STARTED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    raw = event.canonical_line().replace(
        b'"sequence":1', b'"sequence":1,"sequence":1'
    )
    path.write_bytes(raw)

    with pytest.raises(AtesStoreCorruption, match="duplicate JSON object key"):
        AtesEventStore(tmp_path, run_id)


def test_reopen_rejects_noncanonical_but_valid_json(tmp_path: Path):
    run_id = RunId("RUN-noncanonical")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(EventType.RUN_STARTED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    raw = event.canonical_line().replace(b"{", b"{ ", 1)
    path.write_bytes(raw)

    with pytest.raises(AtesStoreCorruption, match="not canonical"):
        AtesEventStore(tmp_path, run_id)


def test_reopen_rejects_event_from_wrong_run(tmp_path: Path):
    run_id = RunId("RUN-expected")
    with AtesEventStore(tmp_path, run_id):
        pass
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    path.write_bytes(_event(RunId("RUN-wrong"), 1).canonical_line())

    with pytest.raises(AtesStoreCorruption, match="expected RUN-expected"):
        AtesEventStore(tmp_path, run_id)


def test_single_authoritative_writer_lock(tmp_path: Path):
    run_id = RunId("RUN-writer-lock")
    first = AtesEventStore(tmp_path, run_id)
    try:
        with pytest.raises(AtesStoreBusy, match="authoritative ATES writer"):
            AtesEventStore(tmp_path, run_id)
    finally:
        first.close()

    with AtesEventStore(tmp_path, run_id):
        pass


def test_uncertain_fsync_poisoning_preserves_identity_for_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run_id = RunId("RUN-fsync")
    store = AtesEventStore(tmp_path, run_id)
    real_fsync = store_module.os.fsync
    evidence_fd = store._file.fileno()

    def fail_evidence_fsync(fd: int) -> None:
        if fd == evidence_fd:
            raise OSError("simulated fsync failure")
        real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(store_module.os, "fsync", fail_evidence_fsync)
        with pytest.raises(AtesAppendError) as caught:
            store.append(
                EventType.RUN_STARTED,
                {"stable": True},
                occurred_at=NOW,
                event_id="EVT-reconcile",
            )
        assert caught.value.outcome_unknown is True
        assert caught.value.event.event_id == EventId("EVT-reconcile")
        assert caught.value.event.sequence == 1
        assert store.poisoned is True
        with pytest.raises(AtesStoreError, match="poisoned"):
            store.append(EventType.TARGET_LAUNCHED, occurred_at=NOW)

    uncertain = caught.value.event
    store.close()
    with AtesEventStore(tmp_path, run_id) as reopened:
        # The complete line was visible after the simulated fsync error. A retry
        # with the same stable identity therefore replays instead of duplicating.
        assert reopened.append_event(uncertain) == uncertain
        assert reopened.next_sequence == 2


def test_closed_store_cannot_append(tmp_path: Path):
    store = AtesEventStore(tmp_path, RunId("RUN-closed"))
    store.close()
    with pytest.raises(AtesStoreError, match="closed"):
        store.append(EventType.RUN_STARTED)


def test_invalid_complete_foreign_fields_fail_closed(tmp_path: Path):
    run_id = RunId("RUN-extra-field")
    with AtesEventStore(tmp_path, run_id) as store:
        event = store.append(EventType.RUN_STARTED, occurred_at=NOW)
    path = _run_path(tmp_path, run_id) / "evidence.jsonl"
    document = json.loads(event.canonical_line())
    document["unexpected"] = "field"
    path.write_bytes(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    with pytest.raises(AtesStoreCorruption, match="unexpected fields"):
        AtesEventStore(tmp_path, run_id)


def test_evidence_file_symlink_is_rejected_where_supported(tmp_path: Path):
    run_id = RunId("RUN-symlink")
    run_dir = _run_path(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    evidence = run_dir / "evidence.jsonl"
    try:
        evidence.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(AtesStoreError, match="symlink or reparse"):
        AtesEventStore(tmp_path, run_id)
