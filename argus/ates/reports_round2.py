"""Round-2 report semantics hardening for PR #22 review findings."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Mapping, Sequence

from .core import RunStatus
from . import reports_runtime as _runtime

_base_model = _runtime._model


def _model(root, approval_key_resolver):
    """Never infer assertion→artifact evidence links from attempt co-residence."""
    model = _base_model(root, approval_key_resolver)
    traceability = model.get("traceability")
    if isinstance(traceability, list):
        for item in traceability:
            if not isinstance(item, dict):
                continue
            # Current canonical artifact events expose only attempt-level
            # ownership.  That is insufficient to prove that an artifact backs
            # a particular assertion/requirement, so leave the assertion-level
            # relationship explicitly unbound until the schema carries one.
            item["artifact_ids"] = []
            item["artifact_binding_state"] = "unbound_no_explicit_assertion_relation"
    return model


def _junit(model: Mapping[str, object]) -> bytes:
    """Render one authoritative testcase whose result equals canonical ATES status.

    Historical attempts remain available in JSON/Markdown/HTML reports, but
    JUnit is consumed as a CI outcome protocol.  Emitting historical retries as
    independently failing testcases can invert the canonical result, so the
    suite contains exactly one synthetic canonical-outcome testcase.
    """
    source = model.get("source", {})
    outcome = model.get("outcome", {})
    run_id = source.get("run_id") if isinstance(source, Mapping) else "unknown"
    status = outcome.get("effective_status") if isinstance(outcome, Mapping) else None
    if status not in {
        RunStatus.PASSED.value,
        RunStatus.FAILED.value,
        RunStatus.ERROR.value,
        RunStatus.CANCELLED.value,
    }:
        status = RunStatus.ERROR.value

    failures = 1 if status == RunStatus.FAILED.value else 0
    errors = 1 if status == RunStatus.ERROR.value else 0
    skipped = 1 if status == RunStatus.CANCELLED.value else 0
    suite = ET.Element(
        "testsuite",
        {
            "name": _runtime._xml_text("Argus ATES " + str(run_id)),
            "tests": "1",
            "failures": str(failures),
            "errors": str(errors),
            "skipped": str(skipped),
        },
    )
    props = ET.SubElement(suite, "properties")
    for name, value in (
        ("ates.evidence_trust", model.get("evidence_trust_state")),
        ("ates.report_trust", model.get("report_trust_state")),
        ("ates.run_status", status),
    ):
        ET.SubElement(
            props,
            "property",
            {"name": name, "value": _runtime._xml_text(value)},
        )

    case = ET.SubElement(
        suite,
        "testcase",
        {
            "classname": "argus.ates.canonical",
            "name": _runtime._xml_text(f"canonical outcome for {run_id}"),
        },
    )
    if status == RunStatus.FAILED.value:
        ET.SubElement(
            case,
            "failure",
            {"message": "canonical ATES run outcome is failed"},
        )
    elif status == RunStatus.ERROR.value:
        ET.SubElement(
            case,
            "error",
            {"message": "canonical ATES run outcome is error"},
        )
    elif status == RunStatus.CANCELLED.value:
        ET.SubElement(
            case,
            "skipped",
            {"message": "canonical ATES run outcome is cancelled"},
        )

    # Preserve diagnostic visibility without giving historical attempts JUnit
    # pass/fail authority.  XML serializers keep this inert and escaped.
    raw_attempts = model.get("attempts", ())
    attempt_count = (
        len(tuple(raw_attempts))
        if isinstance(raw_attempts, Sequence)
        and not isinstance(raw_attempts, (str, bytes, bytearray, Mapping))
        else 0
    )
    ET.SubElement(
        props,
        "property",
        {"name": "ates.historical_attempt_count", "value": str(attempt_count)},
    )

    return (
        ET.tostring(
            suite,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )
        + b"\n"
    )


def install() -> None:
    _runtime._model = _model
    _runtime._junit = _junit


__all__ = ["install"]
