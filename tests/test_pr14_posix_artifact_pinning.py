from __future__ import annotations

import base64
import hashlib
import os

import pytest

from argus.capsule.base import CapsuleError
from argus.capsule.host_collect import collect_file_to_pinned_tree
from argus.capsule.safe_output import pin_artifact_tree


@pytest.mark.skipif(os.name == "nt", reason="descriptor-relative collection is POSIX-specific")
def test_posix_collection_stays_bound_when_parent_path_is_replaced(tmp_path):
    runs_root = tmp_path / ".argus" / "runs"
    runs_root.mkdir(parents=True)
    output = runs_root / "run-1" / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()

    payload = b"bound-to-the-open-directory"
    digest = hashlib.sha256(payload).hexdigest()

    class ReplacingClient:
        def __init__(self):
            self.replaced = False

        def _request(self, method: str, path: str, payload=None) -> dict:
            assert method == "GET"
            assert "/v1/files/collect/chunk?" in path
            if not self.replaced:
                original = output / "logs"
                moved = output / "logs-moved"
                original.rename(moved)
                original.symlink_to(outside, target_is_directory=True)
                self.replaced = True
            return {"data_b64": base64.b64encode(payload_bytes).decode("ascii")}

        def collect_info(self, relative: str) -> dict:
            return {"path": relative, "size": len(payload), "sha256": digest}

    # Keep a separate name so the fake request's payload argument cannot shadow
    # the artifact bytes captured by the closure.
    payload_bytes = payload
    client = ReplacingClient()
    info = {"path": "logs/result.bin", "size": len(payload), "sha256": digest}

    # The lexical identity check intentionally reports the concurrent swap on
    # context exit, but the descriptor-relative write must already have landed
    # in the originally pinned directory and never in the replacement target.
    with pytest.raises(CapsuleError, match="identity changed|disappeared"):
        with pin_artifact_tree(output, ["logs/result.bin"]) as tree:
            result = collect_file_to_pinned_tree(
                client,
                "logs/result.bin",
                tree,
                info=info,
            )
            assert result["sha256"] == digest
            assert (output / "logs-moved" / "result.bin").read_bytes() == payload
            assert not (outside / "result.bin").exists()
