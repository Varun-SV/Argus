"""Privacy-aware mapped artifact collection for Capsule execution environments."""

from __future__ import annotations

from collections.abc import Mapping

from argus.ates import validate_artifact_path
from argus.capsule.files import (
    enforce_total_bytes,
    guest_path_key,
    normalize_guest_relative_path,
)
from argus.capsule.host_collect import collect_file_to_pinned_tree
from argus.execution.base import ExecutionEnvironmentError


def _destination(value: object) -> str:
    raw = str(value or "")
    canonical = validate_artifact_path("artifacts/" + raw)
    return canonical[len("artifacts/"):]


def collect_capsule_artifacts_to_tree(environment, entries, output_tree) -> list[dict]:
    """Collect declared guest sources to opaque caller-selected destinations.

    This is an internal bridge so PR #21 can reuse the existing Capsule guest
    protocol and checksum/change-detection logic without making the Capsule
    choose evidence identities. The caller supplies an ATES-pinned tree and the
    exact destination for every already-declared guest source.
    """
    if not bool(getattr(environment, "_workspace_ready", False)):
        raise ExecutionEnvironmentError("Capsule artifact workspace is not available")
    client = getattr(environment, "_client", None)
    if client is None:
        raise ExecutionEnvironmentError("Capsule guest client is not available")
    if not callable(getattr(client, "collect_info", None)) or not callable(
        getattr(client, "_request", None)
    ):
        raise ExecutionEnvironmentError(
            "Capsule guest client does not support protected streamed artifact collection"
        )

    try:
        snapshot = tuple(entries)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ExecutionEnvironmentError("artifact collection entries could not be snapshotted") from exc

    prepared: list[tuple[str, str, dict]] = []
    seen_guest: set[str] = set()
    seen_destinations: set[str] = set()
    sizes: list[int] = []
    for item in snapshot:
        if not isinstance(item, Mapping):
            raise ExecutionEnvironmentError("artifact collection entries must be mappings")
        guest = normalize_guest_relative_path(str(item.get("path") or ""))
        destination = _destination(item.get("destination"))
        guest_key = guest_path_key(guest)
        destination_key = destination.casefold()
        if guest_key in seen_guest:
            raise ExecutionEnvironmentError("duplicate declared Capsule artifact source")
        if destination_key in seen_destinations:
            raise ExecutionEnvironmentError("duplicate ATES artifact destination")
        seen_guest.add(guest_key)
        seen_destinations.add(destination_key)

        info = dict(client.collect_info(guest))
        size = int(info["size"])
        sizes.append(size)
        enforce_total_bytes(sizes)
        prepared.append((guest, destination, info))

    collected: list[dict] = []
    committed_destinations: list[str] = []
    try:
        for guest, destination, info in prepared:
            data = collect_file_to_pinned_tree(
                client,
                guest,
                output_tree,
                info=info,
                destination_relative=destination,
            )
            committed_destinations.append(destination)
            collected.append(
                {
                    "size": int(data["size"]),
                    "sha256": str(data["sha256"]),
                    # Kept only in transient process memory for diagnostics;
                    # RuntimeArtifactCapture deliberately does not serialize it.
                    "source_path": guest,
                }
            )
    except Exception as exc:
        rollback_errors: list[str] = []
        for destination in reversed(committed_destinations):
            try:
                output_tree.unlink_relative(destination)
            except Exception as rollback_exc:
                rollback_errors.append(type(rollback_exc).__name__)
        detail = "protected Capsule artifact collection failed"
        if rollback_errors:
            detail += "; protected artifact rollback also failed"
        raise ExecutionEnvironmentError(detail) from exc

    return collected


__all__ = ["collect_capsule_artifacts_to_tree"]
