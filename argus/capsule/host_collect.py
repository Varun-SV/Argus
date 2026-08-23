"""Race-safe host-side artifact collection for Capsule hosts."""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.parse
from typing import Optional

from argus.capsule.files import TRANSFER_CHUNK_BYTES, normalize_guest_relative_path
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient


def _write_all(handle, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            raise CapsuleGuestError("host artifact write made no forward progress")
        offset += written


def collect_file_to_pinned_tree(
    client: GuestAgentClient,
    relative: str,
    output_tree,
    *,
    info: Optional[dict] = None,
    destination_relative: Optional[str] = None,
) -> dict:
    """Collect one guest artifact into an already-pinned host artifact tree.

    ``relative`` remains the declared guest-workspace source used for guest API
    requests. ``destination_relative`` is independently selected by the host
    evidence layer; PR #21 uses opaque ArtifactId-derived destinations so guest
    filenames cannot become a metadata side channel.
    """
    relative = normalize_guest_relative_path(relative)
    destination_relative = str(destination_relative or relative)
    metadata = dict(info or client.collect_info(relative))
    size = int(metadata["size"])
    expected = str(metadata["sha256"]).lower()

    destination = output_tree.lexical_path(destination_relative)
    handle, temp_name, _ = output_tree.open_temp_file(destination_relative)
    digest = hashlib.sha256()
    offset = 0
    try:
        with handle:
            while offset < size:
                limit = min(TRANSFER_CHUNK_BYTES, size - offset)
                query = urllib.parse.urlencode(
                    {"path": relative, "offset": offset, "limit": limit}
                )
                data = client._request("GET", f"/v1/files/collect/chunk?{query}")
                try:
                    chunk = base64.b64decode(
                        str(data.get("data_b64") or ""), validate=True
                    )
                except Exception as exc:
                    raise CapsuleGuestError(
                        f"guest returned invalid artifact data for {relative}"
                    ) from exc
                if len(chunk) != limit:
                    raise CapsuleGuestError(
                        f"guest artifact chunk length mismatch for {relative}: "
                        f"expected {limit}, got {len(chunk)}"
                    )
                _write_all(handle, chunk)
                digest.update(chunk)
                offset += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        actual = digest.hexdigest()
        if actual != expected:
            raise CapsuleGuestError(
                f"artifact checksum mismatch for {relative}: "
                f"expected {expected}, got {actual}"
            )

        final_metadata = client.collect_info(relative)
        if (
            int(final_metadata["size"]) != size
            or str(final_metadata["sha256"]).lower() != expected
        ):
            raise CapsuleGuestError(f"artifact changed while being collected: {relative}")

        output_tree.commit_temp(destination_relative, temp_name)
    except Exception:
        output_tree.remove_temp(destination_relative, temp_name)
        raise

    return {
        "path": relative,
        "destination": destination_relative,
        "size": size,
        "sha256": expected,
        "host_path": str(destination),
    }
