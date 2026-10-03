"""Small root-only ISO-9660 writer for ephemeral Argus control media."""

from __future__ import annotations

from pathlib import Path
import os
import stat
from typing import Iterable

from argus.capsule.base import CapsuleError

_SECTOR = 2048


def _both_endian(value: int, width: int) -> bytes:
    return value.to_bytes(width, "little") + value.to_bytes(width, "big")


def _record(extent: int, size: int, identifier: bytes, *, directory: bool) -> bytes:
    identifier_length = len(identifier)
    length = 33 + identifier_length + (identifier_length % 2 == 0)
    data = bytearray(length)
    data[0] = length
    data[2:10] = _both_endian(extent, 4)
    data[10:18] = _both_endian(size, 4)
    data[18:25] = bytes((126, 1, 1, 0, 0, 0, 0))
    data[25] = 2 if directory else 0
    data[28:32] = _both_endian(1, 2)
    data[32] = identifier_length
    data[33:33 + identifier_length] = identifier
    return bytes(data)


def _sector(data: bytes) -> bytes:
    if len(data) > _SECTOR:
        raise CapsuleError("ephemeral ISO structure exceeds one sector")
    return data + b"\x00" * (_SECTOR - len(data))


def write_root_iso(
    output: str | Path,
    *,
    volume_label: str,
    files: Iterable[tuple[str, Path]],
) -> Path:
    """Write one root directory containing regular files."""
    target = Path(output)
    label = volume_label.encode("ascii")
    if not label or len(label) > 32:
        raise CapsuleError("ISO volume label must be 1..32 ASCII bytes")
    entries = []
    next_extent = 24
    seen = set()
    for name, source in files:
        if (
            not isinstance(name, str)
            or not name.endswith(";1")
            or name.upper() != name
            or "/" in name
            or "\\" in name
        ):
            raise CapsuleError("ephemeral ISO member name is invalid")
        encoded = name.encode("ascii")
        if encoded in seen:
            raise CapsuleError("ephemeral ISO member names must be unique")
        seen.add(encoded)
        path = Path(source)
        if path.is_symlink():
            raise CapsuleError("ephemeral ISO source cannot be a symlink")
        try:
            info = path.stat()
        except OSError as exc:
            raise CapsuleError(f"cannot stat ephemeral ISO source: {exc}") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise CapsuleError("ephemeral ISO source must be a nonempty regular file")
        size = int(info.st_size)
        sectors = (size + _SECTOR - 1) // _SECTOR
        entries.append((encoded, path, size, next_extent, sectors))
        next_extent += sectors
    if not entries:
        raise CapsuleError("ephemeral ISO requires at least one file")

    root_record = _record(23, _SECTOR, b"\x00", directory=True)
    directory = (
        root_record
        + _record(23, _SECTOR, b"\x01", directory=True)
        + b"".join(
            _record(extent, size, name, directory=False)
            for name, _path, size, extent, _sectors in entries
        )
    )
    if len(directory) > _SECTOR:
        raise CapsuleError("ephemeral ISO root directory is too large")

    path_table_l = b"\x01\x00" + (23).to_bytes(4, "little") + b"\x01\x00\x00\x00"
    path_table_m = b"\x01\x00" + (23).to_bytes(4, "big") + b"\x00\x01\x00\x00"
    primary = bytearray(_SECTOR)
    primary[:7] = b"\x01CD001\x01"
    primary[8:40] = b"ARGUS".ljust(32, b" ")
    primary[40:72] = label.ljust(32, b" ")
    primary[80:88] = _both_endian(next_extent, 4)
    primary[120:124] = _both_endian(1, 2)
    primary[124:128] = _both_endian(1, 2)
    primary[128:132] = _both_endian(_SECTOR, 2)
    primary[132:140] = _both_endian(len(path_table_l), 4)
    primary[140:144] = (19).to_bytes(4, "little")
    primary[148:152] = (21).to_bytes(4, "big")
    primary[156:190] = root_record
    primary[881] = 1

    created = False
    try:
        with target.open("xb") as stream:
            created = True
            stream.write(b"\x00" * (16 * _SECTOR))
            stream.write(primary)
            stream.write(_sector(b"\xffCD001\x01"))
            stream.write(bytes(_SECTOR))
            stream.write(_sector(path_table_l))
            stream.write(bytes(_SECTOR))
            stream.write(_sector(path_table_m))
            stream.write(bytes(_SECTOR))
            stream.write(_sector(directory))
            for _name, source, size, _extent, sectors in entries:
                written = 0
                with source.open("rb") as src:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                        written += len(chunk)
                if written != size:
                    raise CapsuleError("ephemeral ISO source changed while being copied")
                stream.write(b"\x00" * (sectors * _SECTOR - size))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            target.chmod(0o600)
        return target.resolve()
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
