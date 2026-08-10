from __future__ import annotations

import base64
import hashlib
import os
import urllib.parse

import pytest

from argus.capsule.base import CapsuleError
from argus.capsule.guest import GuestAgentClient
from argus.capsule.safe_output import pin_artifact_tree
from argus.execution.capsule import CapsuleExecutionEnvironment


class _StaticArtifactClient(GuestAgentClient):
    def __init__(self, payload: bytes):
        super().__init__("http://127.0.0.1:1", "bootstrap")
        self.payload = payload
        self.digest = hashlib.sha256(payload).hexdigest()

    def collect_info(self, relative: str) -> dict:
        return {
            "path": relative,
            "size": len(self.payload),
            "sha256": self.digest,
        }

    def _request(self, method: str, path: str, payload=None):
        if path.startswith("/v1/files/collect/chunk?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            offset = int(query["offset"][0])
            limit = int(query["limit"][0])
            chunk = self.payload[offset:offset + limit]
            return {"data_b64": base64.b64encode(chunk).decode("ascii")}
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative collection regression")
def test_posix_collection_cannot_be_redirected_by_parent_replacement(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    output_dir = runs_root / "run-1" / "artifacts"
    relative = "nested/result.bin"
    client = _StaticArtifactClient(b"descriptor-bound artifact")

    moved_parent = output_dir / "nested-original"
    replacement_parent = output_dir / "nested"

    # The lexical identity check intentionally reports the hostile replacement
    # on exit. Before that happens, collection must remain bound to the opened
    # original parent directory and must never write into the replacement.
    with pytest.raises(CapsuleError, match="identity changed"):
        with pin_artifact_tree(output_dir, [relative]) as pinned:
            original_parent = output_dir / "nested"
            original_parent.rename(moved_parent)
            replacement_parent.mkdir()

            data = client.collect_file(relative, pinned, info=client.collect_info(relative))
            assert data["sha256"] == client.digest
            assert (moved_parent / "result.bin").read_bytes() == client.payload
            assert not (replacement_parent / "result.bin").exists()
            assert not list(replacement_parent.glob("*.part"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative rollback regression")
def test_posix_collection_rollback_uses_pinned_parent_fd(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    output_dir = runs_root / "run-2" / "artifacts"
    relative = "nested/result.bin"
    client = _StaticArtifactClient(b"rollback artifact")

    moved_parent = output_dir / "nested-original"
    replacement_parent = output_dir / "nested"

    with pytest.raises(CapsuleError, match="identity changed"):
        with pin_artifact_tree(output_dir, [relative]) as pinned:
            client.collect_file(relative, pinned, info=client.collect_info(relative))
            original_parent = output_dir / "nested"
            original_parent.rename(moved_parent)
            replacement_parent.mkdir()

            errors = CapsuleExecutionEnvironment._rollback_collected_artifacts(
                pinned,
                [{"path": relative}],
            )
            assert errors == []
            assert not (moved_parent / "result.bin").exists()
            assert not (replacement_parent / "result.bin").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative temp cleanup regression")
def test_posix_failed_collection_removes_temp_from_pinned_parent(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    output_dir = runs_root / "run-3" / "artifacts"
    relative = "nested/result.bin"
    client = _StaticArtifactClient(b"actual payload")
    bad_info = {
        "path": relative,
        "size": len(client.payload),
        "sha256": hashlib.sha256(b"different payload").hexdigest(),
    }

    moved_parent = output_dir / "nested-original"
    replacement_parent = output_dir / "nested"

    with pytest.raises(CapsuleError, match="identity changed"):
        with pin_artifact_tree(output_dir, [relative]) as pinned:
            original_parent = output_dir / "nested"
            original_parent.rename(moved_parent)
            replacement_parent.mkdir()

            with pytest.raises(Exception, match="checksum mismatch"):
                client.collect_file(relative, pinned, info=bad_info)

            assert not any(path.name.endswith(".part") for path in moved_parent.iterdir())
            assert not any(path.name.endswith(".part") for path in replacement_parent.iterdir())
            assert not (replacement_parent / "result.bin").exists()
