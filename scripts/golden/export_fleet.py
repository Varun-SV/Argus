"""Export golden Fleet wire messages for compatibility contract C-04.

Specification C-04: "all ``argus-fleet-*-v1`` identifiers and message formats stay identical,
so a fleet can mix Python and Rust nodes during migration." This script runs the real Python
Fleet code through one complete flow with fixed TEST ONLY keys, and records every message that
crosses the Node / Control Center boundary:

* ``expected/messages.json``  for each signed message kind (``node-enrollment``,
  ``node-enrollment-ack``, ``node-heartbeat``, ``node-clock-probe``,
  ``fleet-placement-authorization``, ``fleet-generation-retirement``): the message fields, the
  exact canonical bytes that are signed, the Ed25519 signature (deterministic for a fixed key),
  what the Python verifier decides, and what it decides after one field is changed;
* ``expected/flow.json``  the registry side of the same flow: enrollment results, the heartbeat
  receipt, the clock assessment, the session request payload and digest, the placement record,
  the Node admission state, and the ATES transport binding, batch and receipts;
* ``expected/identifiers.json``  every ``argus-fleet-*`` version identifier in the code;
* ``keys/node.key.json``  a Node private key file in the ``argus-fleet-node-key-v1`` format,
  and ``keys/public.json`` with both public keys. **TEST ONLY** keys from fixed seeds.

Usage::

    python scripts/golden/export_fleet.py --write   # regenerate tests/golden/fleet
    python scripts/golden/export_fleet.py --check   # exit 1 and name every drifting file

Determinism: as ``export_ates.py`` (``scripts/golden/_harness.py``). Every timestamp the flow
passes explicitly is a fixed value, and the keys come from fixed seeds.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.golden import _harness as harness  # noqa: E402 - path set up above
from scripts.golden.export_state import _block_network, render  # noqa: E402

SCHEMA = "argus-golden-fleet-v1"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "fleet"
PINNED_VERSION = "0.0.0+golden"
CONTROL_CENTER_ID = "cc://golden"

SOURCES = (
    "argus/fleet/ates_transport.py",
    "argus/fleet/enrollment.py",
    "argus/fleet/heartbeat.py",
    "argus/fleet/heartbeat_impl.py",
    "argus/fleet/identity.py",
    "argus/fleet/placement.py",
)


def _seed(label: str) -> bytes:
    """32 bytes of TEST ONLY Ed25519 seed material."""
    return hashlib.sha256(f"argus-golden-fleet TEST ONLY {label} key".encode("ascii")).digest()


# --------------------------------------------------------------------------- worker side


def _signed(kind: str, message: Any, verify, tamper: dict[str, Any],
            payload=None) -> dict[str, Any]:
    """Fields, signed bytes, signature and verifier decisions for one signed message.

    ``payload(message)`` gives the signed payload; by default ``message.payload()``.
    """
    from argus.fleet.identity import canonical_signed_message

    canonical = canonical_signed_message(kind, (payload or (lambda m: m.payload()))(message))
    signature_field = "signature" if hasattr(message, "signature") else "proof_signature"
    tampered = []
    for field, value in tamper.items():
        altered = dataclasses.replace(message, **{field: value})
        tampered.append({"field": field, "value": harness.plain(value),
                         "verify": harness.attempt(lambda m=altered: verify(m))})
    return {
        "canonical_message": canonical.decode("utf-8"),
        "canonical_message_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest(),
        "kind": kind,
        "message": harness.plain(message),
        "signature": getattr(message, signature_field),
        "signature_field": signature_field,
        "tampered": tampered,
        "verify": harness.attempt(lambda: verify(message)),
    }


def _text(line: Any) -> str:
    return line.decode("utf-8") if isinstance(line, bytes) else str(line)


def _plain_payload(message: Any) -> Any:
    return harness.plain(message.payload())


def worker(tmp_root: Path) -> int:
    _block_network()
    tmp_root = tmp_root.resolve()
    if not harness.worker_guard(tmp_root):
        return 2
    harness.Determinism("fleet", PINNED_VERSION)

    from argus.ates import AtesEventStore, EventType, RunId
    from argus.fleet import ates_transport, enrollment, heartbeat_impl, identity, placement
    from argus.fleet.ates_transport import FleetAtesAggregator, FleetAtesBatch, FleetAtesRunBinding
    from argus.fleet.enrollment import (
        EnrollmentAcknowledgement,
        EnrollmentRequest,
        FleetEnrollmentRegistry,
    )
    from argus.fleet.heartbeat import (
        CapacityAdvertisement,
        ClockProbeResponse,
        FleetHeartbeatRegistry,
        ImageAdvertisement,
        NodeHeartbeat,
    )
    from argus.fleet.identity import ControlCenterKeyPair, NodeKeyPair
    from argus.fleet.placement import (
        FleetPlacementStore,
        GenerationRetirementAuthorization,
        NodeAdmissionStore,
        PlacementAuthorization,
        SessionRequest,
        StagedInputIdentity,
    )

    out = tmp_root / "out"
    work = tmp_root / "work"
    work.mkdir()
    node_key = NodeKeyPair.from_private_bytes(_seed("node"))
    other_key = NodeKeyPair.from_private_bytes(_seed("other-node"))
    control_key = ControlCenterKeyPair.from_private_bytes(_seed("control-center"))

    # Keys: the on-disk Node key format and both public keys.
    (out / "keys").mkdir(parents=True)
    node_key.save(out / "keys" / "node.key.json")
    (out / "keys" / "public.json").write_text(render({
        "control_center": {"fingerprint": control_key.fingerprint,
                           "public_key_b64": control_key.public_key_b64},
        "node": {"fingerprint": node_key.fingerprint, "public_key_b64": node_key.public_key_b64},
        "note": "TEST ONLY keys derived from fixed seeds in scripts/golden/export_fleet.py",
    }), encoding="utf-8")

    messages: list[dict[str, Any]] = []
    flow: dict[str, Any] = {}
    db = work / "control.sqlite3"

    # Enrollment.
    registry = FleetEnrollmentRegistry(db, control_center_id=CONTROL_CENTER_ID)
    credential = registry.issue_bootstrap_credential(now=100.0, ttl_seconds=60)
    request = EnrollmentRequest.create(control_center_id=CONTROL_CENTER_ID, key_pair=node_key)
    messages.append(_signed(
        "node-enrollment", request, lambda m: m.verify(),
        {"control_center_id": "cc://other", "enrollment_request_id": "ENROLL-" + "0" * 32,
         "public_key_b64": other_key.public_key_b64},
        payload=lambda m: {
            "control_center_id": m.control_center_id,
            "enrollment_request_id": m.enrollment_request_id,
            "public_key_b64": m.public_key_b64,
            "public_key_fingerprint": m.public_key_fingerprint,
        }))
    pending = registry.enroll(request, bootstrap_credential_id=credential.credential_id,
                              bootstrap_secret=credential.secret, now=101.0)
    ack = EnrollmentAcknowledgement.create(result=pending, key_pair=node_key)
    def ack_payload(m):
        return {
            "control_center_id": m.control_center_id,
            "enrollment_request_id": m.enrollment_request_id,
            "node_id": m.node_id,
            "public_key_fingerprint": identity.public_key_fingerprint(m.public_key_b64),
        }

    def verify_ack(m):
        identity.verify_signature(m.public_key_b64,
                                  identity.canonical_signed_message("node-enrollment-ack",
                                                                    ack_payload(m)),
                                  m.proof_signature)

    messages.append(_signed("node-enrollment-ack", ack, verify_ack,
                            {"node_id": "NODE-" + "9" * 32, "control_center_id": "cc://other"},
                            payload=ack_payload))
    active = registry.acknowledge(ack, now=102.0)
    flow["enrollment"] = {
        "bootstrap_credential": {"credential_id": credential.credential_id,
                                 "control_center_id": credential.control_center_id,
                                 "expires_at": credential.expires_at,
                                 "secret": "<TEST ONLY; derived>"},
        "pending": harness.plain(pending),
        "active": harness.plain(active),
        "replayed_acknowledgement": harness.attempt(lambda: registry.acknowledge(ack, now=103.0)),
    }

    # Heartbeat.
    heartbeats = FleetHeartbeatRegistry(db, control_center_id=CONTROL_CENTER_ID)
    image = ImageAdvertisement(alias="win11-qa", image_id="IMG-WIN11-QA-2026-09",
                               digest="sha256:" + "a" * 64, guest_os="windows")
    beat = NodeHeartbeat.create(
        node_id=active.node_id, control_center_id=CONTROL_CENTER_ID, key_pair=node_key,
        boot_id="BOOTID-" + "1" * 32, sequence=1, agent_version="0.1.0", host_os="windows",
        provider="hyperv", capacity=CapacityAdvertisement(4, 1, 32768, 20000, 100000),
        images=(image,), active_session_ids=("SESSION-golden",), node_time=1001.0,
        monotonic_seconds=51.0, heartbeat_id="HEARTBEAT-" + "2" * 32,
    )
    messages.append(_signed("node-heartbeat", beat, lambda m: m.verify_signature(), {
        "sequence": 2, "provider": "libvirt", "node_time": 1002.0,
        "control_center_id": "cc://other",
    }))
    flow["heartbeat"] = {
        "payload": _plain_payload(beat),
        "request_digest": beat.request_digest,
        "receipt": harness.attempt(lambda: heartbeats.accept_heartbeat(beat, received_at=1001.5)),
    }

    # Clock probe.
    challenge = heartbeats.begin_clock_probe(active.node_id, control_sent_at=1000.0)
    probe = ClockProbeResponse.create(
        challenge=challenge, key_pair=node_key, boot_id="BOOTID-" + "1" * 32,
        node_received_at=1000.120, node_sent_at=1000.130, node_monotonic_seconds=50.0,
    )
    messages.append(_signed("node-clock-probe", probe, lambda m: m.verify_signature(), {
        "node_sent_at": 1000.5, "probe_id": challenge.probe_id[:-1] + "0",
    }))
    flow["clock_probe"] = {
        "challenge": harness.plain(challenge),
        "assessment": harness.attempt(
            lambda: heartbeats.complete_clock_probe(probe, control_received_at=1000.050)),
    }

    # Session request, placement and Node admission.
    staged = StagedInputIdentity(
        logical_name="application-under-test", size_bytes=1234,
        transfer_digest="sha256:" + "b" * 64, provenance_method="sha256",
        provenance_value="sha256:" + "b" * 64, sensitive=False,
    )
    run_id = RunId("RUN-" + "3" * 32)
    session = SessionRequest.create(
        session_request_id="SESSION-golden", run_id=run_id, image_digest="sha256:" + "a" * 64,
        spec_digest="sha256:" + "d" * 64, provider="hyperv", guest_os="windows",
        staged_inputs=(staged,), network_policy={"mode": "host_only"},
        isolation_policy={"secure": True},
    )
    placements = FleetPlacementStore(db, control_center_id=CONTROL_CENTER_ID)
    record = placements.create_placement(session, owner_node_id=active.node_id, now=1000.0)
    authorization = PlacementAuthorization.create(record=record,
                                                  control_center_id=CONTROL_CENTER_ID,
                                                  signer=control_key)
    pinned = control_key.public_key_b64

    def verify_authorization(m):
        return m.verify(pinned, control_center_id=CONTROL_CENTER_ID)

    messages.append(_signed("fleet-placement-authorization", authorization, verify_authorization, {
        "placement_generation": 2, "owner_node_id": "NODE-" + "9" * 32,
        "control_center_id": "cc://other", "signer_public_key_b64": node_key.public_key_b64,
    }))
    admission = NodeAdmissionStore(work / "node.sqlite3", node_id=active.node_id,
                                   control_center_id=CONTROL_CENTER_ID,
                                   control_center_public_key_b64=pinned, max_sessions=1)
    flow["placement"] = {
        "session_request_payload": _plain_payload(session),
        "session_request_digest": session.request_digest,
        "record": harness.plain(record),
        "authorization_payload": _plain_payload(authorization),
        "admission": harness.attempt(lambda: admission.admit(session, authorization)),
    }
    retirement = GenerationRetirementAuthorization.create(
        session_request_id=record.session_request_id,
        placement_generation=record.placement_generation, owner_node_id=record.owner_node_id,
        control_center_id=CONTROL_CENTER_ID, signer=control_key,
    )
    messages.append(_signed("fleet-generation-retirement", retirement, verify_authorization, {
        "placement_generation": 7, "session_request_id": "SESSION-other",
    }))

    # ATES transport from the Node's store into the Control Center's aggregator.
    (work / "node-project").mkdir()
    source = AtesEventStore(work / "node-project", run_id)
    source.append(EventType.RUN_STARTED, {"name": "fleet-golden"})
    source.append(EventType.ENVIRONMENT_PREPARED, {"isolated": True})
    source.append(EventType.TARGET_LAUNCHED,
                  {"target": {"disposition": "suppressed", "reason": "golden"}})
    binding = FleetAtesRunBinding.from_placement(record, image_digest="sha256:" + "a" * 64)
    aggregator = FleetAtesAggregator(work / "fleet-ates.sqlite3",
                                     mirror_project_dir=work / "central-mirror",
                                     control_center_id=CONTROL_CENTER_ID)
    batch = FleetAtesBatch.from_store(source)
    flow["ates_transport"] = {
        "version": ates_transport.FLEET_ATES_TRANSPORT_VERSION,
        "binding": harness.plain(binding),
        "bind_run": harness.attempt(lambda: aggregator.bind_run(binding, bound_at=1000.0)),
        "batch_canonical_lines": [_text(event.canonical_line()) for event in batch.events],
        "receipts": harness.attempt(
            lambda: aggregator.ingest_batch(binding, batch, received_at=2000.0)),
        "replayed_batch": harness.attempt(
            lambda: aggregator.ingest_batch(binding, batch, received_at=2001.0)),
        "foreign_node_binding": harness.attempt(lambda: aggregator.bind_run(
            dataclasses.replace(binding, node_id="NODE-" + "9" * 32), bound_at=1000.5)),
    }
    source.close()
    with AtesEventStore(work / "central-mirror", run_id) as mirrored:
        mirror_lines = [_text(event.canonical_line()) for event in mirrored.events]
    flow["ates_transport"]["mirror_matches_source"] = (
        mirror_lines == flow["ates_transport"]["batch_canonical_lines"])

    identifiers = {}
    for module in (ates_transport, enrollment, heartbeat_impl, identity, placement):
        for name, value in sorted(vars(module).items()):
            if isinstance(value, str) and value.startswith("argus-") and value.endswith("-v1"):
                identifiers[f"{module.__name__}.{name}"] = value

    (out / "expected").mkdir()
    for name, value, api in (
        ("messages", messages, "argus.fleet signed messages and their verifiers"),
        ("flow", flow, "argus.fleet registries over one enrollment-to-transport flow"),
        ("identifiers", identifiers, "argus.fleet module-level version identifiers"),
    ):
        (out / "expected" / f"{name}.json").write_text(render({
            "api": api, "case": name, "schema": SCHEMA, "value": value,
        }), encoding="utf-8")
    # Every tampered message must be rejected.
    accepted = [f"{item['kind']}.{change['field']}" for item in messages
                for change in item.get("tampered", []) if "error" not in change["verify"]]
    rejected = [item["kind"] for item in messages if "error" in item["verify"]]
    if accepted or rejected:
        print(f"tampered messages accepted: {accepted}; valid messages rejected: {rejected}",
              file=sys.stderr)
        return 1
    if not flow["ates_transport"]["mirror_matches_source"]:
        print("the ATES mirror does not match the Node's canonical lines", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- parent side


def build_outputs() -> dict[str, bytes]:
    return harness.build_outputs(
        Path(__file__).resolve(), "argus-golden-fleet-",
        lambda files: harness.manifest(
            files, schema=SCHEMA, generator="scripts/golden/export_fleet.py", sources=SOURCES,
            pinned_version=PINNED_VERSION))


def main(argv: list[str] | None = None) -> int:
    return harness.main(argv, doc=__doc__ or "", golden_dir=GOLDEN_DIR,
                        script=Path(__file__).resolve(), worker=worker, build=build_outputs)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.exit(main())
