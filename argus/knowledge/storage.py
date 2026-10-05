"""Stable authority for default project knowledge storage.

Explicit operator-selected persistence directories keep their existing behavior.
Project-default directories reject aliases and remain pinned until store close.
"""
from __future__ import annotations

import os
import threading
import io
import stat
import errno
from contextlib import contextmanager
from pathlib import Path

from argus.ates.store import _PinnedDirectory, _windows_handle_info, AtesStoreError

# Chroma caches systems for the process lifetime by persistence-path string.
# Keep one stable directory authority per physical vector store for that same
# lifetime, rather than letting a later project reuse its descriptor/cache key.
_VECTOR_PINS = {}
_VECTOR_LOCK = threading.Lock()


class KnowledgeFileError(ValueError):
    """Unsafe default knowledge-file authority; never a best-effort failure."""


class _DirectoryFiles:
    """Portable descriptor-relative file operations for project graph storage."""
    def __init__(self, pin):
        self.pin = pin

    @property
    def parent(self):
        return self.pin.path.parent

    def mkdir(self, parents=False, exist_ok=False):
        if not exist_ok:
            raise FileExistsError(self.pin.path)

    def __truediv__(self, name):
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid project knowledge filename")
        return _DirectoryFile(self.pin, name)

    def glob(self, pattern):
        import fnmatch
        for name in os.listdir(self.pin.path if os.name == "nt" else self.pin._fd):
            if fnmatch.fnmatchcase(name, pattern):
                yield self / name


class _DirectoryFile:
    def __init__(self, pin, name):
        self.pin, self.name = pin, name

    @property
    def stem(self):
        return Path(self.name).stem

    @property
    def parent(self):
        return _DirectoryFiles(self.pin)

    def exists(self):
        try:
            info = (os.lstat(self.pin.path / self.name) if os.name == "nt" else
                    os.stat(self.name, dir_fd=self.pin._fd, follow_symlinks=False))
        except FileNotFoundError:
            return False
        self._validate(info)
        return True

    @staticmethod
    def _validate(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                getattr(info, "st_file_attributes", 0) & 0x400):
            raise KnowledgeFileError("Project knowledge must be a singly linked regular file, without reparse points")

    def _assert_identity(self, fd):
        self._validate(os.fstat(fd))
        try:
            self.pin.assert_file_identity(self.name, fd, "project knowledge file")
        except AtesStoreError as exc:
            raise KnowledgeFileError(str(exc)) from exc

    @contextmanager
    def open(self, mode="r", encoding=None):
        flags = {"r": os.O_RDONLY, "w": os.O_WRONLY | os.O_CREAT,
                 "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND}[mode[0]]
        self.exists()
        if os.name == "nt":
            import msvcrt
            # Inspect without write access; a legitimate read-only graph remains readable.
            try:
                kernel, handle, _ = _windows_handle_info(
                    self.pin.path / self.name, directory=False, create=mode[0] != "r", writable=mode[0] != "r")
            except AtesStoreError as exc:
                if "winerror 5" in str(exc):
                    raise PermissionError(str(exc)) from exc
                raise KnowledgeFileError(str(exc)) from exc
            try:
                fd = msvcrt.open_osfhandle(handle, flags & (os.O_RDONLY | os.O_WRONLY | os.O_APPEND))
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        else:
            flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | os.O_NONBLOCK
            try:
                fd = os.open(self.name, flags, 0o600, dir_fd=self.pin._fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR}:
                    raise KnowledgeFileError("Unsafe project knowledge file") from exc
                raise
        stream = None
        try:
            self._assert_identity(fd)
            if mode == "w":
                os.ftruncate(fd, 0)
            stream = io.open(fd, mode=mode, encoding=encoding)
            yield stream
            self._assert_identity(fd)
            stream.flush()
            self._assert_identity(fd)
        finally:
            if stream is None:
                os.close(fd)
            else:
                stream.close()

    def read_text(self, encoding=None):
        with self.open(encoding=encoding) as stream:
            return stream.read()

    def read_bytes(self):
        with self.open("rb") as stream:
            return stream.read()

    def write_text(self, value, encoding=None):
        with self.open("w", encoding=encoding) as stream:
            return stream.write(value)

    def unlink(self):
        self.exists()
        if os.name == "nt":
            os.unlink(self.pin.path / self.name)
        else:
            os.unlink(self.name, dir_fd=self.pin._fd)

    def __str__(self):
        return str(self.pin.path / self.name)


class _VectorPath:
    def __init__(self, pin, path):
        self.pin, self.path = pin, path
        self.retained = False

    def __str__(self):
        info = os.fstat(self.pin._fd) if os.name != "nt" else self.pin.path.stat()
        identity = (info.st_dev, info.st_ino)
        with _VECTOR_LOCK:
            existing = _VECTOR_PINS.get(identity)
            if existing is None:
                _VECTOR_PINS[identity] = self
                self.retained = True
                existing = self
            return str(existing.path)


def _pin(pin):
    if os.name != "nt":
        return pin
    # Metadata-only Windows handles do not deny rename of the directory itself.
    # FILE_LIST_DIRECTORY plus no delete sharing supplies that lifetime fence.
    import ctypes
    kernel = pin._kernel32
    handle = kernel.CreateFileW(str(pin.path), 1, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        pin.close()
        raise OSError(ctypes.get_last_error(), "Cannot protect project knowledge directory")
    try:
        # Validate the object again while the stronger handle denies replacement.
        verified = _PinnedDirectory(pin.path)
        verified.close()
    except BaseException:
        kernel.CloseHandle(handle)
        pin.close()
        raise
    kernel.CloseHandle(pin._win_handle)
    pin._win_handle = handle
    return pin


def _existing_child(pin, name):
    """Open an existing child without initializing project storage."""
    if os.name == "nt":
        return _pin(_PinnedDirectory(pin.path / name))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, dir_fd=pin._fd)
    return _PinnedDirectory._from_posix_fd(pin.path / name, fd)


class _ProjectDirectories:
    def __init__(self, project_dir: Path, *, create=True):
        self.pins = []
        self.vector_path = None
        self.server_pin = None
        try:
            self.project = _pin(_PinnedDirectory(Path(project_dir).resolve(strict=True)))
            self.pins.append(self.project)
            self.argus = (_pin(self.project.ensure_child(".argus", "project data")) if create else
                          _existing_child(self.project, ".argus"))
            self.pins.append(self.argus)
            self.knowledge = (_pin(self.argus.ensure_child("knowledge", "project knowledge")) if create else
                              _existing_child(self.argus, "knowledge"))
            self.pins.append(self.knowledge)
        except BaseException:
            self.close()
            raise

    def path(self, pin):
        if os.name == "nt":
            return pin.path
        # Descriptor-relative directory paths keep backend and vector-library
        # operations anchored even if an ancestor is replaced during a call.
        for base in ("/proc/self/fd",):
            candidate = Path(base) / str(pin._fd)
            if candidate.is_dir():
                return candidate
        return _DirectoryFiles(pin)

    def assert_authoritative(self):
        self.project.assert_child_identity(".argus", self.argus, "project data")
        self.argus.assert_child_identity("knowledge", self.knowledge, "project knowledge")
        if self.vector_path is not None:
            self.knowledge.assert_child_identity("chroma", self.pins[3], "knowledge vectors")
        if self.server_pin is not None:
            self.argus.assert_child_identity("qdrant-data", self.server_pin, "knowledge server storage")

    def pin_vectors(self):
        child = _pin(self.knowledge.ensure_child("chroma", "knowledge vectors"))
        self.pins.append(child)
        self.vector_path = _VectorPath(child, self.path(child))
        return self.vector_path

    def close(self):
        for pin in reversed(self.pins):
            if self.vector_path and self.vector_path.retained and pin is self.vector_path.pin:
                continue
            pin.close()
        self.pins.clear()


class _ProjectKnowledgeStore:
    def __init__(self, store, directories):
        self._store = store
        self._directories = directories
        self._closed = False

    def __getattr__(self, name):
        value = getattr(self._store, name)
        if not callable(value):
            return value

        def guarded(*args, **kwargs):
            if self._closed:
                raise ValueError("Knowledge store is closed")
            self._directories.assert_authoritative()
            result = value(*args, **kwargs)
            self._directories.assert_authoritative()
            return result
        return guarded

    def close(self):
        if self._closed:
            return
        try:
            self._directories.assert_authoritative()
            self._store.close()
            self._directories.assert_authoritative()
        finally:
            self._closed = True
            self._directories.close()

    def __del__(self):
        # Abandoned stores release pins without flushing through unsafe paths.
        if not getattr(self, "_closed", True):
            self._directories.close()


def read_project_knowledge_file(project_dir: Path, name: str) -> bytes:
    directories = _ProjectDirectories(project_dir, create=False)
    try:
        directories.assert_authoritative()
        data = (_DirectoryFiles(directories.knowledge) / name).read_bytes()
        directories.assert_authoritative()
        return data
    finally:
        directories.close()


def create_project_knowledge_store(project_dir: Path, **options):
    from argus.knowledge import _resolve_auto, create_knowledge_store
    from argus.knowledge.store import LocalKnowledgeStore

    directories = _ProjectDirectories(project_dir)
    try:
        argus_path = directories.path(directories.argus)
        if options.get("store_type") == "auto":
            options["store_type"] = _resolve_auto(_DirectoryFiles(directories.argus), interactive=True)
        if isinstance(argus_path, _DirectoryFiles) and options.get("store_type") in {"local", "docker", "qdrant"}:
            raise ValueError("Secure default Chroma/Docker storage is unavailable on this platform. "
                             "Set knowledge.type to json, or explicitly configure an operator-approved "
                             "knowledge.persist_dir for the backend you need.")
        if options.get("store_type") in {"docker", "qdrant"}:
            # Docker resolves mounts on its host, not in the calling process's
            # descriptor namespace. Keep its canonical data location and reject
            # aliases of the writable storage before starting the backend.
            qdrant = _pin(directories.argus.ensure_child("qdrant-data", "knowledge server storage"))
            directories.pins.append(qdrant)
            directories.server_pin = qdrant
            argus_path = directories.argus.path
            options["docker_storage_path"] = (Path(f"/proc/{os.getpid()}/fd/{qdrant._fd}")
                                               if os.name != "nt" and Path("/proc/self/fd").is_dir()
                                               else qdrant.path)
        store = create_knowledge_store(
            persist_dir=_DirectoryFiles(directories.knowledge), data_dir=argus_path, **options)
        if isinstance(store, LocalKnowledgeStore):
            store._chroma_path = directories.pin_vectors()
        directories.assert_authoritative()
        return _ProjectKnowledgeStore(store, directories)
    except BaseException:
        directories.close()
        raise
