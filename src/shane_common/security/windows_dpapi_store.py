"""Windows DPAPI-backed ``SecretStore`` (application-neutral; promoted from Purity's implementation, which is left untouched).

Secrets are encrypted with ``CryptProtectData`` (current-user scope) and persisted in a single JSON file (base64 ciphertext values only -- the plaintext secret is never
written to disk). Decrypting succeeds only under the Windows user that encrypted it. The caller supplies the application id (``%LOCALAPPDATA%\<app_id>\secrets.dat``) or an
explicit path; there is no default application.
"""

from __future__ import annotations

import base64
import ctypes
import json
from ctypes import wintypes
from pathlib import Path

from shane_common.io.atomic import write_json_atomic
from shane_common.preferences.paths import app_settings_path
from shane_common.security.secret_store import SecretStoreDecryptionError, SecretStoreError

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_protect(data: bytes) -> bytes:
    in_blob = _make_blob(data)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(  # type: ignore[attr-defined]
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreError(f"CryptProtectData failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)  # type: ignore[attr-defined]


def _dpapi_unprotect(data: bytes) -> bytes:
    in_blob = _make_blob(data)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
        ctypes.byref(in_blob), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreDecryptionError(f"CryptUnprotectData failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)  # type: ignore[attr-defined]


def _key(namespace: str, name: str) -> str:
    return f"{namespace}\x00{name}"


class WindowsDpapiSecretStore:
    """DPAPI-backed ``SecretStore``, current-user scoped."""

    def __init__(self, *, app_id: str | None = None, path: Path | None = None) -> None:
        if path is None and not app_id:
            raise ValueError("WindowsDpapiSecretStore needs an app_id or an explicit path")
        self._path = Path(path) if path is not None else app_settings_path(app_id, "secrets.dat")

    def set_secret(self, namespace: str, name: str, value: str) -> None:
        data = self._load_raw()
        ciphertext = _dpapi_protect(value.encode("utf-8"))
        data[_key(namespace, name)] = base64.b64encode(ciphertext).decode("ascii")
        write_json_atomic(self._path, data)

    def get_secret(self, namespace: str, name: str) -> str | None:
        data = self._load_raw()
        encoded = data.get(_key(namespace, name))
        if encoded is None:
            return None
        ciphertext = base64.b64decode(encoded.encode("ascii"))
        return _dpapi_unprotect(ciphertext).decode("utf-8")

    def delete_secret(self, namespace: str, name: str) -> None:
        data = self._load_raw()
        if _key(namespace, name) in data:
            del data[_key(namespace, name)]
            write_json_atomic(self._path, data)

    def _load_raw(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretStoreError(f"Could not read secret store at {self._path}: {exc}") from exc
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SecretStoreError(f"Secret store at {self._path} is corrupt: {exc}") from exc
        if not isinstance(data, dict):
            raise SecretStoreError(f"Secret store at {self._path} has an unexpected format.")
        return data
