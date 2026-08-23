from __future__ import annotations

import pytest

from argus.ates import FinalizationError, finalize_revision_one, recover_revision_one
from tests.test_ates_finalization import _open_run


def test_bound_recovery_rejects_partial_tail_without_repairing_evidence(tmp_path):
    store = _open_run(tmp_path)
    run_id = store.run_id
    try:
        result = finalize_revision_one(store)
    finally:
        store.close()

    evidence = result.run_dir / "evidence.jsonl"
    canonical = evidence.read_bytes()
    partial_tail = b'{"post_finalization_tamper":true'
    assert not partial_tail.endswith(b"\n")
    with evidence.open("ab") as handle:
        handle.write(partial_tail)
        handle.flush()

    tampered = canonical + partial_tail
    assert evidence.read_bytes() == tampered
    assert result.binding_path.exists()

    with pytest.raises(
        FinalizationError,
        match="authoritative run state|verification|canonical evidence|trailing",
    ):
        recover_revision_one(tmp_path, run_id)

    # A bound package is immutable from the recovery API. Recovery must not
    # truncate/heal post-finalization corruption back to manifest-bound bytes.
    assert evidence.read_bytes() == tampered
    assert result.binding_path.exists()
