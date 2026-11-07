"""Export golden Capsule guest-protocol files for compatibility contract C-05.

Specification C-05: "the host must keep working with images whose guest runtime is the Python
PyInstaller bundle, until the operator republishes images with the native ``argus-guest``. The
runtime bundle manifest gains a runtime kind [...]. ``python-pyinstaller`` manifests stay
byte-identical and omit it." This script writes, with the real Python code, the files that cross
the host / guest boundary and records what the real readers decide about them:

* ``runtime-bundle/``  a guest runtime bundle (``argus-guest-runtime-bundle-v1``) built by
  ``create_guest_runtime_bundle`` from a tiny TEST ONLY payload, and its
  ``argus-runtime-manifest.json`` as stored inside the ZIP;
* ``bootstrap/``  one bootstrap attempt (``argus-bootstrap-v1``) from ``create_bootstrap_attempt``:
  ``bootstrap.json``, ``bootstrap.token`` and ``tls-cert.pem``. The private key is not exported;
* ``control/``  the host's Capsule control record (``argus-capsule-control-v1``) after a
  generation is reserved, committed and the Capsule becomes active, and the guest's control state
  (``argus-guest-control-v1``);
* ``expected/*.json``  the readers' decisions on those files and on modified copies, including
  a manifest that carries the C-05 ``runtime_kind`` field (which the Python line rejects, as C-05
  expects).

Usage::

    python scripts/golden/export_capsule.py --write   # regenerate tests/golden/capsule
    python scripts/golden/export_capsule.py --check   # exit 1 and name every drifting file

Determinism: as ``export_ates.py`` (``scripts/golden/_harness.py``), with one exception. The
bootstrap TLS certificate is signed with ECDSA, which is randomized, so it cannot be reproduced.
The certificate is therefore made once by the real ``_certificate_identity`` and then reused:
when ``bootstrap/tls-cert.pem`` is already committed, the worker returns it from
``_certificate_identity`` instead of signing a new one. Everything else in the attempt is still
written by ``create_bootstrap_attempt``. Delete the committed certificate to make a new one.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.golden import _harness as harness  # noqa: E402 - path set up above
from scripts.golden.export_state import _block_network, render  # noqa: E402

SCHEMA = "argus-golden-capsule-v1"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "capsule"
PINNED_VERSION = "0.0.0+golden"
CAPSULE_ID = "cap-" + "c" * 32
COMMITTED_CERT = GOLDEN_DIR / "bootstrap" / "tls-cert.pem"
KEY_PLACEHOLDER = b"TEST ONLY: the golden export does not keep this private key\n"

SOURCES = (
    "argus/capsule/bootstrap.py",
    "argus/capsule/control.py",
    "argus/capsule/files.py",
    "argus/provisioning/model.py",
    "argus/provisioning/runtime_bundle.py",
)


def _expected(out: Path, name: str, api: str, value: Any) -> None:
    path = out / "expected" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render({"api": api, "case": name, "schema": SCHEMA, "value": value}),
                    encoding="utf-8")


def _runtime_bundle(out: Path, work: Path) -> str:
    from argus.provisioning.model import GuestRuntimeIdentity
    from argus.provisioning.runtime_bundle import (
        BUNDLE_MANIFEST_NAME,
        GuestRuntimeBundleManifest,
        create_guest_runtime_bundle,
        verify_guest_runtime_bundle,
    )

    payload = work / "payload"
    (payload / "argus-guest" / "_internal").mkdir(parents=True)
    (payload / "argus-guest" / "argus-guest.exe").write_bytes(
        b"TEST ONLY: stand-in for the PyInstaller guest runtime entrypoint\n")
    (payload / "argus-guest" / "_internal" / "base_library.zip").write_bytes(
        b"TEST ONLY: stand-in for a PyInstaller support file\n")
    bundle_dir = out / "runtime-bundle"
    bundle = bundle_dir / "argus-guest-runtime.zip"
    result = create_guest_runtime_bundle(
        payload, bundle, runtime_version="0.1.0-golden", target_os="windows-11",
        target_architecture="x86_64", entrypoint="argus-guest/argus-guest.exe",
    )
    with zipfile.ZipFile(bundle) as archive:
        manifest_bytes = archive.read(BUNDLE_MANIFEST_NAME)
        members = [(info.filename, info.date_time, info.external_attr >> 16, info.compress_type)
                   for info in archive.infolist()]
    (bundle_dir / BUNDLE_MANIFEST_NAME).write_bytes(manifest_bytes)
    bundle_sha256 = hashlib.sha256(bundle.read_bytes()).hexdigest()

    def identity(**changes: Any) -> GuestRuntimeIdentity:
        fields = {"bundle_path": str(bundle), "runtime_bundle_sha256": bundle_sha256,
                  "runtime_version": "0.1.0-golden", "target_os": "windows-11",
                  "target_architecture": "x86_64"}
        fields.update(changes)
        return GuestRuntimeIdentity(**fields)

    replace = {str(bundle_dir): "<BUNDLE_DIR>", str(work): "<WORK>"}

    def verify(ident: GuestRuntimeIdentity, path: Path | None = None) -> dict[str, Any]:
        return harness.attempt(lambda: verify_guest_runtime_bundle(
            ident, allowed_roots=[bundle_dir, work], path=path), replace)

    # A bundle whose payload member changed but whose manifest did not.
    altered = work / "altered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(altered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith("base_library.zip"):
                data = data.replace(b"support", b"SUPPORT")
            target.writestr(info, data)
    altered_sha = hashlib.sha256(altered.read_bytes()).hexdigest()
    manifest = json.loads(manifest_bytes)
    _expected(out, "runtime-bundle", "argus.provisioning.runtime_bundle", {
        "build_result": harness.attempt(lambda: result, replace),
        "bundle_sha256": bundle_sha256,
        "members": [list(item) for item in members],
        "verify": verify(identity()),
        "verify_rejections": {
            "wrong_outer_digest": verify(identity(runtime_bundle_sha256="0" * 64)),
            "wrong_runtime_version": verify(identity(runtime_version="0.2.0")),
            "wrong_target_os": verify(identity(target_os="ubuntu")),
            "member_changed_manifest_not": verify(
                identity(bundle_path=str(altered), runtime_bundle_sha256=altered_sha),
                path=altered),
        },
        "manifest_from_mapping": {
            "as_written": harness.attempt(
                lambda: GuestRuntimeBundleManifest.from_mapping(manifest)),
            "with_runtime_kind_native": harness.attempt(
                lambda: GuestRuntimeBundleManifest.from_mapping(
                    {**manifest, "runtime_kind": "native"})),
            "with_runtime_kind_python_pyinstaller": harness.attempt(
                lambda: GuestRuntimeBundleManifest.from_mapping(
                    {**manifest, "runtime_kind": "python-pyinstaller"})),
            "unsupported_format_version": harness.attempt(
                lambda: GuestRuntimeBundleManifest.from_mapping(
                    {**manifest, "format_version": "argus-guest-runtime-bundle-v2"})),
        },
    })
    return bundle_sha256


def _bootstrap(out: Path, work: Path, bundle_sha256: str, det: harness.Determinism) -> None:
    import argus.capsule.bootstrap as bootstrap

    real_identity = bootstrap._certificate_identity

    def certificate_identity(capsule_id: str, generation: int) -> tuple[bytes, bytes, str]:
        if COMMITTED_CERT.is_file():
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization

            pem = COMMITTED_CERT.read_bytes()
            der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
            return pem, KEY_PLACEHOLDER, hashlib.sha256(der).hexdigest()
        # A new certificate reads the clock and os.urandom; restore the deterministic state
        # afterwards, so the rest of the export is the same whether the certificate was made
        # now or reused.
        saved = det.snapshot()
        try:
            return real_identity(capsule_id, generation)
        finally:
            det.restore(saved)

    bootstrap._certificate_identity = certificate_identity
    attempt = bootstrap.create_bootstrap_attempt(
        work / "bootstrap-parent", capsule_id=CAPSULE_ID, control_generation=1,
        execution_mode="isolated", runtime_identity="runtime-sha256-" + bundle_sha256,
    )
    dest = out / "bootstrap"
    dest.mkdir(parents=True)
    for path in (attempt.manifest_path, attempt.token_path, attempt.tls_cert_path):
        shutil.copyfile(path, dest / path.name)
    raw = json.loads(attempt.manifest_path.read_text(encoding="utf-8"))

    def load_with(changes: dict[str, Any] | None = None, drop: str | None = None):
        root = work / f"bootstrap-variant-{len(list(work.glob('bootstrap-variant-*')))}"
        root.mkdir()
        shutil.copyfile(attempt.tls_cert_path, root / "tls-cert.pem")
        data = {k: v for k, v in raw.items() if k != drop}
        data.update(changes or {})
        (root / "bootstrap.json").write_text(json.dumps(data, sort_keys=True,
                                                        separators=(",", ":")) + "\n",
                                             encoding="utf-8")
        return harness.attempt(lambda: bootstrap.load_bootstrap_manifest(root))

    _expected(out, "bootstrap", "argus.capsule.bootstrap", {
        "schema_version": bootstrap.BOOTSTRAP_SCHEMA_VERSION,
        "load": harness.attempt(lambda: bootstrap.load_bootstrap_manifest(attempt.root)),
        "load_rejections": {
            "certificate_digest_changed": load_with({"tls_cert_sha256": "0" * 64}),
            "unknown_field": load_with({"runtime_kind": "native"}),
            "schema_version_v2": load_with({"schema_version": "argus-bootstrap-v2"}),
            "generation_zero": load_with({"control_generation": 0}),
            "token_file_with_path": load_with({"token_file": "../bootstrap.token"}),
            "created_at_missing": load_with(drop="created_at"),
        },
        "token_length": len(attempt.bootstrap_token),
    })


def _control(out: Path, work: Path) -> None:
    from argus.capsule.control import (
        CapsuleControlRecord,
        CapsuleControlRegistry,
        CapsuleLifecycleState,
        GuestControlState,
        GuestControlStateStore,
    )

    registry = CapsuleControlRegistry(work / "host-control")
    record = CapsuleControlRecord(
        capsule_id=CAPSULE_ID, provider="hyperv",
        provider_resource_identity="vm-guid:11111111-2222-3333-4444-555555555555",
        mutable_disk_identity="sha256:" + hashlib.sha256(b"session-disk").hexdigest(),
        environment_id="env-sha256-" + "a" * 64, base_image_sha256="b" * 64,
        lifecycle_state=CapsuleLifecycleState.ALLOCATED.value,
        network_policy_identity="host_only:v1",
    )
    registry.create(record)
    _, first = registry.reserve_generation(CAPSULE_ID)
    _, second = registry.reserve_generation(CAPSULE_ID)
    committed = registry.commit_generation(CAPSULE_ID, second, execution_mode="isolated")
    stale = harness.attempt(
        lambda: registry.commit_generation(CAPSULE_ID, first, execution_mode="isolated"))
    active = registry.transition(CAPSULE_ID, CapsuleLifecycleState.ACTIVE.value)
    host_dir = out / "control" / "host"
    host_dir.mkdir(parents=True)
    shutil.copyfile(work / "host-control" / f"{CAPSULE_ID}.json", host_dir / f"{CAPSULE_ID}.json")

    store = GuestControlStateStore(work / "guest" / "control.json")
    state = store.commit_generation(capsule_id=CAPSULE_ID, generation=second,
                                    session_id="session-golden", execution_mode="isolated")
    replay = harness.attempt(lambda: store.commit_generation(
        capsule_id=CAPSULE_ID, generation=second, session_id="session-replay",
        execution_mode="isolated"))
    guest_dir = out / "control" / "guest"
    guest_dir.mkdir(parents=True)
    shutil.copyfile(work / "guest" / "control.json", guest_dir / "control.json")

    record_raw = json.loads((host_dir / f"{CAPSULE_ID}.json").read_text(encoding="utf-8"))
    guest_raw = json.loads((guest_dir / "control.json").read_text(encoding="utf-8"))
    _expected(out, "control", "argus.capsule.control", {
        "reserved_generations": [first, second],
        "committed": harness.plain(committed),
        "stale_commit": stale,
        "active": harness.plain(active),
        "guest_state": harness.plain(state),
        "guest_replayed_generation": replay,
        "record_from_mapping": {
            "as_written": harness.attempt(lambda: CapsuleControlRecord.from_mapping(record_raw)),
            "record_version_v2": harness.attempt(lambda: CapsuleControlRecord.from_mapping(
                {**record_raw, "record_version": "argus-capsule-control-v2"})),
            "unknown_lifecycle_state": harness.attempt(lambda: CapsuleControlRecord.from_mapping(
                {**record_raw, "lifecycle_state": "paused"})),
        },
        "guest_from_mapping": {
            "as_written": harness.attempt(lambda: GuestControlState.from_mapping(guest_raw)),
            "schema_version_v2": harness.attempt(lambda: GuestControlState.from_mapping(
                {**guest_raw, "schema_version": "argus-guest-control-v2"})),
            "identity_without_generation": harness.attempt(lambda: GuestControlState(
                **{**dataclasses.asdict(state), "highest_committed_generation": 0})),
        },
    })


def worker(tmp_root: Path) -> int:
    _block_network()
    tmp_root = tmp_root.resolve()
    if not harness.worker_guard(tmp_root):
        return 2
    det = harness.Determinism("capsule", PINNED_VERSION)
    out = tmp_root / "out"
    work = tmp_root / "work"
    work.mkdir()
    bundle_sha256 = _runtime_bundle(out, work)
    _bootstrap(out, work, bundle_sha256, det)
    _control(out, work)
    for name in ("runtime-bundle", "bootstrap", "control"):
        text = (out / "expected" / f"{name}.json").read_text(encoding="utf-8")
        if str(tmp_root) in text:
            print(f"expected/{name}.json contains a temporary path", file=sys.stderr)
            return 1
    return 0


# --------------------------------------------------------------------------- parent side


def build_outputs() -> dict[str, bytes]:
    return harness.build_outputs(
        Path(__file__).resolve(), "argus-golden-capsule-",
        lambda files: harness.manifest(
            files, schema=SCHEMA, generator="scripts/golden/export_capsule.py", sources=SOURCES,
            pinned_version=PINNED_VERSION))


def main(argv: list[str] | None = None) -> int:
    return harness.main(argv, doc=__doc__ or "", golden_dir=GOLDEN_DIR,
                        script=Path(__file__).resolve(), worker=worker, build=build_outputs)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.exit(main())
