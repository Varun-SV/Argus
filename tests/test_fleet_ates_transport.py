from __future__ import annotations

from dataclasses import dataclass

import pytest

from argus.ates import AtesEventStore, EventType, RunId
from argus.fleet.ates_transport import (
    FleetAtesAggregator,
    FleetAtesBatch,
    FleetAtesConflict,
    FleetAtesGap,
    FleetAtesRunBinding,
)
from argus.fleet.placement import PlacementRecord


def _placement(run_id: RunId, *, node="NODE-" + ("1" * 32)):
    return PlacementRecord(
        session_request_id="SESSION-test",
        run_id=str(run_id),
        request_digest="sha256:" + ("a" * 64),
        owner_node_id=node,
        placement_generation=1,
        state="running",
        dispatch_operation_id="DISPATCH-" + ("2" * 32),
        cancellation_state="none",
        cancellation_operation_id=None,
    )


def _binding(run_id: RunId, *, node="NODE-" + ("1" * 32)):
    return FleetAtesRunBinding.from_placement(
        _placement(run_id, node=node),
        image_digest="sha256:" + ("b" * 64),
    )


def _events(tmp_path, run_id):
    source = AtesEventStore(tmp_path / "source", run_id)
    first = source.append(EventType.RUN_STARTED, {"name": "fleet-test"})
    second = source.append(EventType.ENVIRONMENT_PREPARED, {"isolated": True})
    third = source.append(EventType.TARGET_LAUNCHED, {"target": {"disposition": "suppressed", "reason": "test"}})
    return source, (first, second, third)


def _aggregator(tmp_path):
    return FleetAtesAggregator(
        tmp_path / "fleet-ates.sqlite3",
        mirror_project_dir=tmp_path / "central-mirror",
        control_center_id="cc://test",
    )


def test_exact_canonical_events_are_mirrored_without_rewriting(tmp_path):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding, bound_at=1000.0)

    batch = FleetAtesBatch.from_store(source)
    receipts = aggregator.ingest_batch(binding, batch, received_at=2000.0)

    assert [item.sequence for item in receipts] == [1, 2, 3]
    assert aggregator.committed_sequence(run_id) == 3
    source_lines = [event.canonical_line() for event in events]
    source.close()

    with AtesEventStore(tmp_path / "central-mirror", run_id) as mirrored:
        assert [event.canonical_line() for event in mirrored.events] == source_lines


def test_exact_replay_preserves_first_control_center_receipt_time(tmp_path):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding, bound_at=1000.0)

    first = aggregator.ingest_event(binding, events[0], received_at=2000.0)
    replay = aggregator.ingest_event(binding, events[0], received_at=9000.0)

    assert replay == first
    assert replay.first_received_at == 2000.0
    source.close()


def test_gap_is_explicit_and_pending_event_reconciles_after_missing_sequence(tmp_path):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding)

    with pytest.raises(FleetAtesGap) as gap:
        aggregator.ingest_event(binding, events[1], received_at=2000.0)
    assert gap.value.expected_sequence == 1
    assert aggregator.committed_sequence(run_id) == 0
    assert aggregator.receipts(run_id)[0].state == "pending"

    aggregator.ingest_event(binding, events[0], received_at=2001.0)
    second = aggregator.ingest_event(binding, events[1], received_at=9999.0)
    assert aggregator.committed_sequence(run_id) == 2
    assert second.first_received_at == 2000.0
    source.close()


def test_conflicting_event_content_for_same_sequence_fails_closed(tmp_path):
    run_id = RunId.new()
    source1 = AtesEventStore(tmp_path / "source1", run_id)
    original = source1.append(
        EventType.RUN_STARTED,
        {"name": "original"},
        event_id="EVT-" + ("1" * 32),
    )
    source1.close()

    source2 = AtesEventStore(tmp_path / "source2", run_id)
    conflict = source2.append(
        EventType.RUN_STARTED,
        {"name": "different"},
        event_id="EVT-" + ("2" * 32),
    )
    source2.close()

    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding)
    aggregator.ingest_event(binding, original, received_at=1000.0)

    with pytest.raises(FleetAtesConflict, match="conflicting"):
        aggregator.ingest_event(binding, conflict, received_at=1001.0)


def test_run_binding_cannot_move_between_nodes_or_generations(tmp_path):
    run_id = RunId.new()
    aggregator = _aggregator(tmp_path)
    first = _binding(run_id)
    aggregator.bind_run(first)

    moved = FleetAtesRunBinding(
        run_id=run_id,
        session_request_id=first.session_request_id,
        node_id="NODE-" + ("9" * 32),
        placement_generation=2,
        placement_request_digest=first.placement_request_digest,
        image_digest=first.image_digest,
    )
    with pytest.raises(FleetAtesConflict, match="different Fleet provenance"):
        aggregator.bind_run(moved)


@dataclass(frozen=True)
class _Clock:
    node_minus_control_offset_ms: float = 125.0
    uncertainty_ms: float = 20.0
    sample_age_ms: float = 400.0


def test_clock_metadata_is_transport_provenance_not_event_mutation(tmp_path):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    canonical_before = events[0].canonical_line()
    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding)

    receipt = aggregator.ingest_event(
        binding,
        events[0],
        received_at=2000.0,
        clock_assessment=_Clock(),
    )

    assert receipt.clock_offset_ms == 125.0
    assert receipt.clock_uncertainty_ms == 20.0
    assert receipt.clock_sample_age_ms == 400.0
    with AtesEventStore(tmp_path / "central-mirror", run_id) as mirror:
        assert mirror.events[0].canonical_line() == canonical_before
    source.close()


def test_restart_repairs_pending_receipt_by_exact_event_replay(tmp_path, monkeypatch):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    binding = _binding(run_id)
    aggregator = _aggregator(tmp_path)
    aggregator.bind_run(binding)

    real_connect = aggregator._connect
    calls = {"count": 0}

    class _FailingCommitConnection:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def execute(self, sql, params=()):
            if "UPDATE fleet_ates_receipts" in sql and "state='committed'" in sql:
                raise RuntimeError("simulated receipt commit crash")
            return self._inner.execute(sql, params)

    def flaky_connect():
        calls["count"] += 1
        conn = real_connect()
        # First connection is pending receipt, second is post-canonical commit.
        if calls["count"] == 2:
            return _FailingCommitConnection(conn)
        return conn

    monkeypatch.setattr(aggregator, "_connect", flaky_connect)
    with pytest.raises(RuntimeError, match="simulated"):
        aggregator.ingest_event(binding, events[0], received_at=2000.0)

    reopened = _aggregator(tmp_path)
    replay = reopened.ingest_event(binding, events[0], received_at=9000.0)
    assert replay.state == "committed"
    assert replay.first_received_at == 2000.0
    assert reopened.committed_sequence(run_id) == 1
    source.close()


def test_batch_cursor_only_selects_events_after_committed_sequence(tmp_path):
    run_id = RunId.new()
    source, events = _events(tmp_path, run_id)
    batch = FleetAtesBatch.from_store(source, after_sequence=1, limit=1)
    assert [event.sequence for event in batch.events] == [2]
    source.close()
