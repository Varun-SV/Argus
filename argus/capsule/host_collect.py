"""Race-safe host-side artifact collection for POSIX Capsule hosts."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse
from typing import Optional

from argus.capsule.files import TRANSFER_CHUNK_BYTES, normalize_guest_relative_path
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient
from argus.capsule.safe_output import PinnedArtifactTree


def collect_file_to_pinned_tree(
    client: GuestAgentClient,
    relative: str,
    output_tree: PinnedArtifactTree,
    *,
    info: Optional[dict] = None,
) -> dict:
    """Collect one artifact using only descriptor-relative host mutations.

    ``pin_artifact_tree`` has already opened and attested every destination
    parent.  This helper deliberately keeps all temporary-file creation,
    commit, and cleanup relative to those opened directory descriptors so a
    concurrent POSIX rename/replacement cannot redirect artifact bytes.
    """
    relative = normalize_guest_relative_path(relative)
    metadata = dict(info or client.collect_info(relative))
    size = int(metadata["size"])
    expected = str(metadata["sha256"]).lower()

    destination = output_tree.lexical_path(relative)
    handle, temp_name, _ = output_tree.open_temp_file(relative)
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
                handle.write(chunk)
                digest.update(chunk)
                offset += len(chunk)

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

        output_tree.commit_temp(relative, temp_name)
    except Exception:
        output_tree.remove_temp(relative, temp_name)
        raise

    return {
        "path": relative,
        "size": size,
        "sha256": expected,
        "host_path": str(destination),
    }
