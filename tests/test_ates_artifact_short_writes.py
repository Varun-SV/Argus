import pytest

import argus.ates.artifacts as artifact_io
import argus.capsule.host_collect as capsule_io
from argus.ates import ArtifactCaptureError
from argus.capsule.guest import CapsuleGuestError


class _ShortWriter:
    def __init__(self, width=2):
        self.width = width
        self.data = bytearray()

    def write(self, value):
        chunk = bytes(value[: self.width])
        self.data.extend(chunk)
        return len(chunk)


class _StalledWriter:
    def write(self, value):
        return 0


def test_ates_artifact_write_all_handles_short_writes():
    writer = _ShortWriter()
    artifact_io._write_all(writer, b"abcdefg")
    assert bytes(writer.data) == b"abcdefg"


def test_ates_artifact_write_all_rejects_no_progress():
    with pytest.raises(ArtifactCaptureError, match="no forward progress"):
        artifact_io._write_all(_StalledWriter(), b"secret")


def test_capsule_mapped_write_all_handles_short_writes():
    writer = _ShortWriter(width=1)
    capsule_io._write_all(writer, b"abcdefg")
    assert bytes(writer.data) == b"abcdefg"


def test_capsule_mapped_write_all_rejects_no_progress():
    with pytest.raises(CapsuleGuestError, match="no forward progress"):
        capsule_io._write_all(_StalledWriter(), b"secret")
