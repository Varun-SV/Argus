"""Privacy-aware mapped artifact collection for Capsule execution environments."""

from __future__ import annotations

from collections.abc import Mapping

from argus.ates import validate_artifact_path
from argus.capsule.files import (
    TRANSFER_MAX_FILE_BYTES,
    TRANSFER_MAX_TOTAL_BYTES,
    enforce_total_bytes,
    guest_path_key,
    normalize_guest_relative_path,
)
from argus.capsule.host_collect import collect_file_to_pinned_tree
from argus.execution.base import ExecutionEnvironmentError


class ArtifactCollectionPreflightError(ExecutionEnvironmentError):
    """Secret-safe structured failure for one declared collection ordinal."""

    _REASONS = frozenset({"artifact.capture_unavailable", "artifact.too_large"})

    def __init__(self, *, suppression_reason: str, collection_ordinal: int) -> None:
        reason = str(suppression_reason or "")
        if reason not in self._REASONS:
            raise ValueError("unsupported collection preflight suppression reason")
        if (
            isinstance(collection_ordinal, bool)
            or not isinstance(collection_ordinal, int)
            or collection_ordinal <= 0
        ):
            raise ValueError("collection preflight ordinal must be a positive integer")
        self.suppression_reason = reason
        self.collection_ordinal = collection_ordinal
        super().__init__(
            "protected Capsule artifact collection preflight failed; "
            "declared artifact is missing, invalid, or unavailable"
        )


def _destination(value: object) -> str:
    raw = str(value or "")
    canonical = validate_artifact_path("artifacts/" + raw)
    return canonical[len("artifacts/"):]


def _collect_info_failure_reason(exc: BaseException) -> str:
    """Map built-in guest diagnostics to a closed reason without retaining them.

    ``GuestAgentClient.collect_info`` rejects an oversize file before returning
    its metadata. Its exception contains the guest path, so the bridge must not
    propagate or store that text. The numeric suffix is inspected transiently
    only to preserve the oversize classification; every other failure collapses
    to the generic unavailable code.
    """
    text = str(exc)
    if text.startswith("guest artifact size is invalid for "):
        _prefix, separator, suffix = text.rpartition(":")
        if separator:
            try:
                size = int(suffix.strip())
            except ValueError:
                pass
            else:
                if size > TRANSFER_MAX_FILE_BYTES:
                    return "artifact.too_large"
    return "artifact.capture_unavailable"


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
        raise ExecutionEnvironmentError(
            "artifact collection entries could not be snapshotted"
        ) from exc

    prepared: list[tuple[str, str, dict]] = []
    seen_guest: set[str] = set()
    seen_destinations: set[str] = set()
    sizes: list[int] = []
    for ordinal, item in enumerate(snapshot, 1):
        try:
            if not isinstance(item, Mapping):
                raise ValueError("artifact collection entry is invalid")
            guest = normalize_guest_relative_path(str(item.get("path") or ""))
            destination = _destination(item.get("destination"))
            guest_key = guest_path_key(guest)
            destination_key = destination.casefold()
            if guest_key in seen_guest:
                raise ValueError("duplicate declared Capsule artifact source")
            if destination_key in seen_destinations:
                raise ValueError("duplicate ATES artifact destination")
            seen_guest.add(guest_key)
            seen_destinations.add(destination_key)

            try:
                info = dict(client.collect_info(guest))
            except Exception as exc:
                raise ArtifactCollectionPreflightError(
                    suppression_reason=_collect_info_failure_reason(exc),
                    collection_ordinal=ordinal,
                ) from exc
            size = int(info["size"])
            if size < 0:
                raise ValueError("artifact size is invalid")
            if size > TRANSFER_MAX_FILE_BYTES:
                raise ArtifactCollectionPreflightError(
                    suppression_reason="artifact.too_large",
                    collection_ordinal=ordinal,
                )
            sizes.append(size)
            if sum(sizes) > TRANSFER_MAX_TOTAL_BYTES:
                raise ArtifactCollectionPreflightError(
                    suppression_reason="artifact.too_large",
                    collection_ordinal=ordinal,
                )
            enforce_total_bytes(sizes)
            prepared.append((guest, destination, info))
        except ArtifactCollectionPreflightError:
            raise
        except Exception as exc:
            raise ArtifactCollectionPreflightError(
                suppression_reason="artifact.capture_unavailable",
                collection_ordinal=ordinal,
            ) from exc

    collected: list[dict] = []
    attempted_destinations: list[str] = []
    try:
        for guest, destination, info in prepared:
            # A final name can become visible before the helper returns, so the
            # current destination must already belong to the outer rollback set.
            attempted_destinations.append(destination)
            data = collect_file_to_pinned_tree(
                client,
                guest,
                output_tree,
                info=info,
                destination_relative=destination,
            )
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
        rollback_errors: list[BaseException] = []
        for destination in reversed(attempted_destinations):
            try:
                output_tree.unlink_relative(destination)
            except FileNotFoundError:
                pass
            except BaseException as rollback_exc:
                rollback_errors.append(rollback_exc)
        if rollback_errors:
            raise ExecutionEnvironmentError(
                "protected Capsule artifact collection failed; "
                "protected artifact rollback was incomplete or ambiguous"
            ) from rollback_errors[0]
        raise ExecutionEnvironmentError("protected Capsule artifact collection failed") from exc

    return collected


__all__ = ["ArtifactCollectionPreflightError", "collect_capsule_artifacts_to_tree"]
