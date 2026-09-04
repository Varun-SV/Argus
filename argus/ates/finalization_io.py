"""Pinned finalization I/O, exact-byte bindings, and durable publication."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional

from .finalization_types import FinalizationError, _finalization_error
from .store import (
    AtesStoreError,
    _PinnedDirectory,
    _open_regular_file,
    _validate_regular_file_descriptor,
    _windows_handle_info,
)

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")



def _json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError) as exc:
        raise FinalizationError(f"cannot serialize canonical finalization JSON: {exc}") from exc


def _read(path: Path) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"invalid finalization JSON file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"finalization JSON file {path.name} must contain an object")
    return value


def _write_all(handle, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = handle.write(view[offset:])
        if isinstance(written, bool) or not isinstance(written, int) or written <= 0:
            raise FinalizationError("finalization write made no forward progress")
        offset += written


def _publish_no_overwrite(directory: _PinnedDirectory, name: str, data: bytes) -> Path:
    if not _SAFE_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise FinalizationError("invalid finalization filename")
    temp_name = f".{name}.argus-{uuid.uuid4().hex}.part"
    handle = None
    final_created = False
    try:
        handle, created = _open_regular_file(directory, temp_name)
        if not created:
            raise FinalizationError("finalization temporary filename already exists")
        with handle:
            _write_all(handle, data)
            handle.flush()
            os.fsync(handle.fileno())
        handle = None

        if os.name == "nt":
            try:
                os.rename(directory.path / temp_name, directory.path / name)
            except FileExistsError as exc:
                raise FinalizationError(f"finalization file already exists: {name}") from exc
            final_created = True
        else:
            if directory._fd is None:  # pragma: no cover - defensive
                raise FinalizationError("pinned finalization directory has no descriptor")
            try:
                os.link(
                    temp_name,
                    name,
                    src_dir_fd=directory._fd,
                    dst_dir_fd=directory._fd,
                    follow_symlinks=False,
                )
                final_created = True
                os.unlink(temp_name, dir_fd=directory._fd)
            except FileExistsError as exc:
                raise FinalizationError(f"finalization file already exists: {name}") from exc
        directory.fsync()
        return directory.path / name
    except BaseException as exc:
        cleanup_error: Optional[BaseException] = None
        try:
            if os.name == "nt":
                (directory.path / temp_name).unlink(missing_ok=True)
            elif directory._fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=directory._fd)
                except FileNotFoundError:
                    pass
        except BaseException as item:
            cleanup_error = item
        if final_created:
            try:
                if os.name == "nt":
                    (directory.path / name).unlink(missing_ok=True)
                elif directory._fd is not None:
                    try:
                        os.unlink(name, dir_fd=directory._fd)
                    except FileNotFoundError:
                        pass
                directory.fsync()
            except BaseException as item:
                cleanup_error = cleanup_error or item
        if cleanup_error is not None:
            raise FinalizationError(
                "finalization publication failed and cleanup became ambiguous"
            ) from cleanup_error
        raise exc


def _pinned_bytes(directory, name, label):
    path = directory.path / name
    if os.name == "nt":
        try: kernel32, raw, _ = _windows_handle_info(path, directory=False, create=False)
        except (OSError, AtesStoreError) as exc: raise FinalizationError(f"{label} is unavailable") from exc
        keep = raw
        try:
            import msvcrt
            fd = msvcrt.open_osfhandle(raw, os.O_RDONLY | getattr(os, "O_BINARY", 0)); keep = None
            try:
                _validate_regular_file_descriptor(fd, path)
                with os.fdopen(fd, "rb", buffering=0) as handle: return handle.read()
            except BaseException:
                try: os.close(fd)
                except OSError: pass
                raise
        finally:
            if keep is not None: kernel32.CloseHandle(keep)
    if directory._fd is None: raise FinalizationError(f"pinned authority unavailable for {label}")
    try: fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory._fd)
    except OSError as exc: raise FinalizationError(f"{label} is unavailable") from exc
    try:
        _validate_regular_file_descriptor(fd, path)
        with os.fdopen(fd, "rb", buffering=0, closefd=False) as handle: data = handle.read()
        directory.assert_file_identity(name, fd, label); return data
    except (OSError, AtesStoreError) as exc: raise FinalizationError(f"{label} cannot be verified safely") from exc
    finally: os.close(fd)


def _assert_directory_identity(directory, label: str) -> None:
    if os.name == "nt":
        return
    if directory._fd is None:
        _finalization_error(f"pinned authority unavailable for {label}")
    try:
        named = os.stat(directory.path, follow_symlinks=False)
        pinned = os.fstat(directory._fd)
    except OSError as exc:
        _finalization_error(f"{label} namespace cannot be verified", exc)
    if (
        not stat.S_ISDIR(named.st_mode)
        or (named.st_dev, named.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        _finalization_error(
            f"{label} namespace no longer refers to the pinned directory"
        )


def _durable_remove(directory, name: str) -> None:
    try:
        if os.name == "nt":
            try:
                (directory.path / name).unlink()
            except FileNotFoundError:
                pass
        else:
            if directory._fd is None:
                _finalization_error(
                    "pinned authority unavailable during publication rollback"
                )
            try:
                os.unlink(name, dir_fd=directory._fd)
            except FileNotFoundError:
                pass
        directory.fsync()

        if os.name == "nt":
            try:
                (directory.path / name).lstat()
            except FileNotFoundError:
                pass
            else:
                _finalization_error(

                    f"published finalization member still exists after rollback: {name}",
                )
        else:
            try:
                os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _finalization_error(

                    f"published finalization member still exists after rollback: {name}",
                )
        _assert_directory_identity(directory, "finalization directory")
    except FinalizationError:
        raise
    except BaseException as exc:
        _finalization_error(

            f"durable rollback of published finalization member failed: {name}",
            exc,
        )


def _strict_json_object(raw: bytes, label: str) -> Mapping[str, object]:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _finalization_error(f"{label} is not strict JSON", exc)
    if not isinstance(value, dict):
        _finalization_error(f"{label} must contain an object")
    if _json(value) != raw:
        _finalization_error(
            f"{label} is not in canonical persisted representation"
        )
    return value


def _entry_exists(directory, name: str) -> bool:
    try:
        if os.name == "nt":
            (directory.path / name).lstat()
        else:
            if directory._fd is None:
                _finalization_error(
                    "pinned authority unavailable while inspecting recovery state"
                )
            os.stat(name, dir_fd=directory._fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except FinalizationError:
        raise
    except OSError as exc:
        _finalization_error(
            f"recovery member {name} cannot be inspected safely", exc
        )


def _publish(directory, name, data):
    expected_size = len(data)
    expected_digest = hashlib.sha256(data).digest()
    _assert_directory_identity(directory, "finalization directory")
    path = _publish_no_overwrite(directory, name, data)
    try:
        _assert_directory_identity(directory, "finalization directory")
        actual = _pinned_bytes(
            directory, name, f"finalization member {name}"
        )
        if (
            len(actual) != expected_size
            or not hmac.compare_digest(
                hashlib.sha256(actual).digest(), expected_digest
            )
            or actual != data
        ):
            _finalization_error(

                f"published finalization member differs from source bytes: {name}",
            )
        directory.fsync()
        _assert_directory_identity(directory, "finalization directory")
        confirmed = _pinned_bytes(
            directory, name, f"finalization member {name}"
        )
        if confirmed != data:
            _finalization_error(

                f"published finalization member changed after durability barrier: {name}",
            )
        return path
    except BaseException as primary:
        try:
            _durable_remove(directory, name)
        except BaseException as cleanup:
            raise FinalizationError(
                "finalization publication verification failed and durable "
                f"rollback is incomplete or ambiguous: {name}"
            ) from cleanup
        raise primary


def _json_object(raw: bytes, label: str):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FinalizationError(
            f"{label} is not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise FinalizationError(f"{label} must contain an object")
    if _json(value) != raw:
        raise FinalizationError(
            f"{label} is not in canonical persisted representation"
        )
    return value


def _member_by_path(members, path: str, label: str):
    if (
        isinstance(members, (str, bytes, bytearray, Mapping))
        or not isinstance(members, Sequence)
    ):
        raise FinalizationError(f"{label} members are malformed")
    matches = [
        item
        for item in tuple(members)
        if isinstance(item, Mapping) and item.get("path") == path
    ]
    if len(matches) != 1:
        raise FinalizationError(
            f"{label} must contain exactly one member for {path}"
        )
    return matches[0]


def _expect_file_digest(meta, raw: bytes, label: str) -> None:
    if not isinstance(meta, Mapping):
        raise FinalizationError(f"{label} metadata is malformed")
    expected_size = meta.get("size_bytes")
    expected_digest = meta.get("sha256")
    if expected_size is not None:
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise FinalizationError(f"{label} size is invalid")
        if len(raw) != expected_size:
            raise FinalizationError(f"{label} size does not match binding")
    if not isinstance(expected_digest, str) or not expected_digest.startswith(
        "sha256:"
    ):
        raise FinalizationError(f"{label} digest is invalid")
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, expected_digest):
        raise FinalizationError(f"{label} digest does not match binding")


def _preflight_bound_members(root: Path) -> None:
    """Verify exact persisted finalization member bytes using pinned/no-follow handles."""
    run_pin = None
    manifests = None
    try:
        run_pin = _PinnedDirectory(root)
        manifests = _PinnedDirectory(root / "manifests")
        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )

        binding_raw = _pinned_bytes(
            run_pin, "run.json", "finalization binding"
        )
        manifest_raw = _pinned_bytes(
            manifests, "manifest-0001.json", "evidence manifest"
        )
        package_raw = _pinned_bytes(
            manifests, "package-manifest-0001.json", "package manifest"
        )
        evidence_raw = _pinned_bytes(
            run_pin, "evidence.jsonl", "canonical evidence"
        )

        binding = _json_object(binding_raw, "run.json")
        manifest = _json_object(manifest_raw, "manifest-0001.json")
        package = _json_object(package_raw, "package-manifest-0001.json")

        bound_manifests = binding.get("manifests")
        if not isinstance(bound_manifests, Mapping):
            raise FinalizationError(
                "run binding manifest metadata is malformed"
            )
        evidence_binding = bound_manifests.get("evidence")
        package_binding = bound_manifests.get("package")
        if (
            not isinstance(evidence_binding, Mapping)
            or evidence_binding.get("path")
            != "manifests/manifest-0001.json"
        ):
            raise FinalizationError(
                "run binding evidence-manifest path is invalid"
            )
        if (
            not isinstance(package_binding, Mapping)
            or package_binding.get("path")
            != "manifests/package-manifest-0001.json"
        ):
            raise FinalizationError(
                "run binding package-manifest path is invalid"
            )
        _expect_file_digest(
            evidence_binding, manifest_raw, "bound evidence manifest"
        )
        _expect_file_digest(
            package_binding, package_raw, "bound package manifest"
        )

        evidence_meta = manifest.get("evidence")
        if (
            not isinstance(evidence_meta, Mapping)
            or evidence_meta.get("path") != "evidence.jsonl"
        ):
            raise FinalizationError(
                "evidence manifest member metadata is malformed"
            )
        _expect_file_digest(
            evidence_meta, evidence_raw, "canonical evidence"
        )

        package_evidence = _member_by_path(
            package.get("members"),
            "evidence.jsonl",
            "package manifest",
        )
        package_manifest = _member_by_path(
            package.get("members"),
            "manifests/manifest-0001.json",
            "package manifest",
        )
        _expect_file_digest(
            package_evidence, evidence_raw, "package evidence member"
        )
        _expect_file_digest(
            package_manifest,
            manifest_raw,
            "package evidence-manifest member",
        )

        run_pin.assert_child_identity(
            "manifests", manifests, "ATES manifests directory"
        )
        _assert_directory_identity(run_pin, "ATES run directory")
        _assert_directory_identity(
            manifests, "ATES manifests directory"
        )
    except FinalizationError:
        raise
    except (OSError, AtesStoreError, ValueError) as exc:
        raise FinalizationError(
            "finalization members cannot be verified safely"
        ) from exc
    finally:
        if manifests is not None:
            try:
                manifests.close()
            except BaseException:
                pass
        if run_pin is not None:
            try:
                run_pin.close()
            except BaseException:
                pass
