"""Round-6 detached audit schema validation for PR #22."""
from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime

from . import audit as _api
from . import audit_impl as _impl


def _aware_timestamp(value: object, index: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _impl.ApprovalError(
            f"audit record {index} has invalid occurred_at"
        )
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset() if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise _impl.ApprovalError(
            f"audit record {index} has invalid occurred_at"
        ) from exc
    if offset is None:
        raise _impl.ApprovalError(
            f"audit record {index} occurred_at must be timezone-aware"
        )


def validate_audit_chain(run_dir):
    """Validate both the hash chain and the complete canonical audit-row shape."""
    root = _impl._run_root(run_dir)
    records = _impl._read_jsonl(root, "audit.jsonl")
    previous = None
    seen: set[str] = set()

    for index, record in enumerate(records, 1):
        if not isinstance(record, Mapping):
            raise _impl.ApprovalError(f"audit record {index} is malformed")
        if record.get("ledger_version") != _impl.AUDIT_LEDGER_VERSION:
            raise _impl.ApprovalError(
                f"audit record {index} has unsupported ledger version"
            )

        audit_id = record.get("audit_id")
        if (
            not isinstance(audit_id, str)
            or not _impl._AUDIT_ID_RE.fullmatch(audit_id)
            or audit_id in seen
        ):
            raise _impl.ApprovalError(
                f"audit record {index} has invalid/duplicate audit_id"
            )

        for field in ("event_type", "actor"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _impl.ApprovalError(
                    f"audit record {index} has invalid {field}"
                )

        _aware_timestamp(record.get("occurred_at"), index)

        if "details" not in record or not isinstance(record.get("details"), Mapping):
            raise _impl.ApprovalError(
                f"audit record {index} has invalid details"
            )

        if "dedupe_key" not in record:
            raise _impl.ApprovalError(
                f"audit record {index} is missing dedupe_key"
            )
        dedupe_key = record.get("dedupe_key")
        if dedupe_key is not None and (
            not isinstance(dedupe_key, str) or not dedupe_key.strip()
        ):
            raise _impl.ApprovalError(
                f"audit record {index} has invalid dedupe_key"
            )

        if "previous_record_digest" not in record:
            raise _impl.ApprovalError(
                f"audit record {index} is missing previous_record_digest"
            )
        if record.get("previous_record_digest") != previous:
            raise _impl.ApprovalError(
                f"audit record {index} breaks the append hash chain"
            )

        seen.add(audit_id)
        previous = _impl._audit_digest(record)

    return records


def install() -> None:
    _api.validate_audit_chain = validate_audit_chain
    _impl.validate_audit_chain = validate_audit_chain
    parent = sys.modules.get(__package__)
    if parent is not None:
        setattr(parent, "validate_audit_chain", validate_audit_chain)


__all__ = ["install", "validate_audit_chain"]
