"""PR #22 recovery hardening for canonical run namespaces and partial tails."""
from __future__ import annotations

from pathlib import Path

from . import finalization_round3 as _round3
from .core import EventType
from .ids import RunId
from .store import _run_directory_key


def _preflight_recovery_members(project_dir, run_id, impl) -> None:
    """Verify crash-state members before recovery publishes new state.

    AtesEventStore owns both the canonical RunId→directory encoding and the
    narrow repair contract for an unterminated trailing JSONL record. Reuse
    those authorities instead of reconstructing the run pathname or opening
    evidence with stricter semantics than the recovery path itself.
    """
    try:
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_id must be a valid RunId") from exc
    try:
        project = Path(project_dir).resolve(strict=True)
    except OSError as exc:
        _round3._finalization_error(
            impl, "recovery project directory is unavailable", exc
        )

    # Do not create a new run merely to preflight recovery. The existence probe
    # must use the same encoding as _RunDirectoryChain so supported uppercase
    # and underscore RunIds cannot skip exact-byte validation.
    root = project / ".argus" / "runs" / _run_directory_key(rid)
    if not root.exists():
        return

    store = None
    manifests = None
    try:
        # This may trim only an unterminated trailing record. That tail is not a
        # canonical event; no new manifest, completion, or binding is published
        # until the persisted candidate below has been proven byte-for-byte.
        store = impl.AtesEventStore(
            project,
            rid,
            repair_trailing_partial=True,
        )
        directories = store._directories
        if directories is None:
            _round3._finalization_error(
                impl, "run authority unavailable during recovery preflight"
            )
        directories.assert_authoritative()
        run_pin = directories.run
        root = store.run_dir

        if _round3._entry_exists(run_pin, "run.json", impl):
            return
        if not _round3._entry_exists(run_pin, "manifests", impl):
            return

        try:
            manifests = impl._PinnedDirectory(root / "manifests")
        except impl.AtesStoreError as exc:
            _round3._finalization_error(
                impl, "ATES manifests recovery namespace is unsafe", exc
            )
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        if not _round3._entry_exists(manifests, "manifest-0001.json", impl):
            return

        manifest_raw = impl._pinned_bytes(
            manifests,
            "manifest-0001.json",
            "recovery evidence manifest",
        )
        manifest = _round3._strict_json_object(
            manifest_raw,
            "recovery evidence manifest",
            impl,
        )

        outcome, completion = impl._candidate_from_manifest(manifest, rid)
        finals = [
            event
            for event in store.events
            if event.envelope.event_type is EventType.RUN_COMPLETED
        ]
        if finals:
            if (
                len(finals) != 1
                or store.events[-1].canonical_line() != completion.canonical_line()
            ):
                _round3._finalization_error(
                    impl, "existing completion differs from recovery candidate"
                )
            pre = store.events[:-1]
        else:
            pre = store.events

        if completion.sequence != len(pre) + 1:
            _round3._finalization_error(
                impl, "recovery completion sequence is inconsistent"
            )

        state = impl._derive(pre, rid)
        if (
            outcome.status_policy_version != impl.STATUS_POLICY_VERSION
            or impl.derive_run_status(state.status_inputs)
            is not outcome.effective_status
        ):
            _round3._finalization_error(
                impl, "recovery outcome differs from canonical derivation"
            )

        artifacts = impl._artifacts(store, state.artifacts)
        expected_manifest, expected_package, expected_evidence = impl._documents(
            pre,
            completion,
            outcome,
            artifacts,
        )
        if manifest_raw != impl._json(expected_manifest):
            _round3._finalization_error(
                impl,
                "recovery evidence manifest bytes differ from regenerated candidate",
            )

        if _round3._entry_exists(
            manifests,
            "package-manifest-0001.json",
            impl,
        ):
            package_raw = impl._pinned_bytes(
                manifests,
                "package-manifest-0001.json",
                "recovery package manifest",
            )
            _round3._strict_json_object(
                package_raw,
                "recovery package manifest",
                impl,
            )
            if package_raw != impl._json(expected_package):
                _round3._finalization_error(
                    impl,
                    "recovery package manifest bytes differ from regenerated candidate",
                )

        if finals and store._read_all() != expected_evidence:
            _round3._finalization_error(
                impl, "recovered evidence differs from manifest-bound candidate"
            )

        directories.assert_authoritative()
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        _round3._assert_directory_identity(
            run_pin,
            "ATES run directory",
            impl,
        )
        _round3._assert_directory_identity(
            manifests,
            "ATES manifests directory",
            impl,
        )
    except impl.FinalizationError:
        raise
    except (OSError, impl.AtesStoreError, ValueError) as exc:
        _round3._finalization_error(
            impl, "recovery members cannot be preflighted safely", exc
        )
    finally:
        if manifests is not None:
            try:
                manifests.close()
            except BaseException:
                pass
        if store is not None:
            try:
                store.close()
            except BaseException:
                pass


def install() -> None:
    """Replace round-3's preflight while preserving its recovery wrapper."""
    _round3._preflight_recovery_members = _preflight_recovery_members
