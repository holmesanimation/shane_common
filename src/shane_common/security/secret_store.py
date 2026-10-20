"""Generic secret-storage abstraction (promoted from Purity's neutral pattern; no application imports).

Namespace + name identify a secret (e.g. namespace ``"trading_system.dropbox"``, name ``"refresh_token"``). Values are strings. Implementations must never store secrets in
plaintext, never log secret values, and never fall back silently to an insecure store.
"""

from __future__ import annotations

from typing import Protocol


class SecretStoreError(Exception):
    """Base error for secret store failures."""


class SecretStoreDecryptionError(SecretStoreError):
    """A stored secret cannot be decrypted (wrong user, corruption, ...)."""


class SecretStore(Protocol):
    def set_secret(self, namespace: str, name: str, value: str) -> None: ...

    def get_secret(self, namespace: str, name: str) -> str | None: ...

    def delete_secret(self, namespace: str, name: str) -> None: ...


class InMemorySecretStore:
    """Test double; never persisted."""

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], str] = {}

    def set_secret(self, namespace: str, name: str, value: str) -> None:
        self._d[(namespace, name)] = value

    def get_secret(self, namespace: str, name: str) -> str | None:
        return self._d.get((namespace, name))

    def delete_secret(self, namespace: str, name: str) -> None:
        self._d.pop((namespace, name), None)
