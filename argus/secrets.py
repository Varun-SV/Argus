"""Per-user Argus secret storage for runtime-only credential references.

The database is kept in the user's application-data directory. Values are
encrypted before SQLite sees them: Windows uses user-scoped DPAPI; other hosts
use AES-GCM with a separate, mode-0600 local key. The latter protects copied
database files, while the local account and directory permissions protect the
key itself. This store is deliberately not an ATES evidence sink.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_REF = re.compile(r"^secret://argus/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class SecretStoreError(RuntimeError):
    """A secret cannot be safely stored or resolved."""


def validate_secret_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref.split("/"):
        raise SecretStoreError("secret reference must be a secret://argus/ name")
    return ref


def default_secret_dir() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise SecretStoreError("APPDATA is required for the Argus secret store")
        return Path(appdata) / "Argus" / "Secrets"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Argus" / "Secrets"
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else Path.home() / ".local" / "share"
    if not root.is_absolute():
        raise SecretStoreError("XDG_DATA_HOME must be absolute")
    return root / "argus" / "secrets"


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.argtypes = (
        [ctypes.POINTER(DataBlob), ctypes.POINTER(wintypes.LPWSTR),
         ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
         wintypes.DWORD, ctypes.POINTER(DataBlob)]
        if decrypt else
        [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.POINTER(DataBlob),
         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
         ctypes.POINTER(DataBlob)]
    )
    method.restype = wintypes.BOOL
    args = (
        (ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob))
        if decrypt else
        (ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob))
    )
    if not method(*args):
        raise SecretStoreError("Windows could not protect or resolve an Argus secret")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


class ArgusSecretStore:
    """A local per-user store with atomic set/replace and reference lookup."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_secret_dir()
        if not self.root.is_absolute():
            raise SecretStoreError("secret store path must be absolute")
        self.root = self.root.expanduser()

    def _prepare_dir(self) -> None:
        if self.root.is_symlink():
            raise SecretStoreError("secret store directory must not be a symlink")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise SecretStoreError("secret store path is not a directory")
            if os.name != "nt":
                self.root.chmod(0o700)
        except OSError as exc:
            raise SecretStoreError("cannot prepare Argus secret store") from exc

    def _regular_private_file(self, path: Path, *, create: bool = False) -> None:
        if create and not path.exists() and not path.is_symlink():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise SecretStoreError("cannot create Argus secret store file") from exc
        try:
            info = path.lstat()
        except OSError as exc:
            raise SecretStoreError("Argus secret store file is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
            raise SecretStoreError("Argus secret store file has unsafe permissions or type")

    def _key(self) -> bytes:
        path = self.root / "secrets.key"
        if not path.exists() and not path.is_symlink():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                pass
            except OSError as exc:
                raise SecretStoreError("cannot create Argus secret key") from exc
        self._regular_private_file(path)
        key = path.read_bytes()
        if len(key) != 32:
            raise SecretStoreError("Argus secret key is invalid")
        return key

    def _connect(self) -> sqlite3.Connection:
        self._prepare_dir()
        path = self.root / "secrets.sqlite3"
        self._regular_private_file(path, create=True)
        try:
            connection = sqlite3.connect(path, timeout=30)
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                "ref TEXT PRIMARY KEY, protected BLOB NOT NULL, updated_at TEXT NOT NULL)"
            )
            return connection
        except sqlite3.Error as exc:
            raise SecretStoreError("cannot open Argus secret store") from exc

    def _protect(self, ref: str, value: str) -> bytes:
        raw = value.encode("utf-8")
        if os.name == "nt":
            return b"D" + _dpapi(raw, decrypt=False)
        nonce = secrets.token_bytes(12)
        return b"A" + nonce + AESGCM(self._key()).encrypt(nonce, raw, ref.encode("utf-8"))

    def _unprotect(self, ref: str, payload: bytes) -> str:
        try:
            if os.name == "nt" and payload[:1] == b"D":
                raw = _dpapi(payload[1:], decrypt=True)
            elif os.name != "nt" and payload[:1] == b"A" and len(payload) > 13:
                raw = AESGCM(self._key()).decrypt(
                    payload[1:13], payload[13:], ref.encode("utf-8")
                )
            else:
                raise SecretStoreError("Argus secret format is unsupported")
            return raw.decode("utf-8")
        except (UnicodeError, ValueError, InvalidTag) as exc:
            raise SecretStoreError("Argus secret cannot be resolved") from exc

    def set(self, ref: str, value: str) -> None:
        ref = validate_secret_ref(ref)
        if not isinstance(value, str) or not value:
            raise SecretStoreError("secret value must be nonempty text")
        self._prepare_dir()
        protected = self._protect(ref, value)
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO secrets(ref, protected, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(ref) DO UPDATE SET protected=excluded.protected, "
                        "updated_at=excluded.updated_at",
                        (ref, protected, stamp),
                    )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise SecretStoreError("cannot save Argus secret") from exc

    def get(self, ref: str) -> str:
        ref = validate_secret_ref(ref)
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT protected FROM secrets WHERE ref=?", (ref,)
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise SecretStoreError("cannot resolve Argus secret") from exc
        if row is None:
            raise SecretStoreError("Argus secret reference was not found")
        return self._unprotect(ref, row[0])

    def list_refs(self) -> tuple[str, ...]:
        try:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT ref FROM secrets ORDER BY ref").fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise SecretStoreError("cannot list Argus secret references") from exc
        return tuple(row[0] for row in rows)

    def delete(self, ref: str) -> bool:
        ref = validate_secret_ref(ref)
        try:
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute("DELETE FROM secrets WHERE ref=?", (ref,))
                    return cursor.rowcount != 0
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise SecretStoreError("cannot delete Argus secret") from exc
