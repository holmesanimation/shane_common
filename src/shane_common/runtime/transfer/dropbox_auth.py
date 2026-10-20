"""Application-neutral Dropbox OAuth (PKCE, offline access, refresh token) -- M5-1.

The user opens the authorize URL, approves the app and pastes the one-time auth code; the code is exchanged for a refresh token which is the ONLY persisted credential
(DPAPI-protected via a ``SecretStore``). No app secret is needed or stored with PKCE. The SDK client refreshes short-lived access tokens automatically from the refresh token;
access tokens are never persisted. Namespaces are caller-supplied so each application has its own independent credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from shane_common.security.secret_store import SecretStore

NAME_REFRESH_TOKEN = "refresh_token"


class DropboxCredentialStore:
    def __init__(self, store: SecretStore, namespace: str) -> None:
        if not namespace:
            raise ValueError("namespace is required (each application owns its own credential)")
        self._store, self._ns = store, namespace

    def get_refresh_token(self) -> Optional[str]:
        return self._store.get_secret(self._ns, NAME_REFRESH_TOKEN)

    def set_refresh_token(self, value: str) -> None:
        self._store.set_secret(self._ns, NAME_REFRESH_TOKEN, value)

    def clear(self) -> None:
        self._store.delete_secret(self._ns, NAME_REFRESH_TOKEN)


@dataclass
class DropboxAuthResult:
    refresh_token: str = ""
    account_id: Optional[str] = None


class DropboxAuthFlow:
    """One in-progress authorization attempt (PKCE, offline)."""

    def __init__(self, app_key: str, *, flow: Any = None, scopes: Optional[list] = None) -> None:
        if flow is None:
            from dropbox import DropboxOAuth2FlowNoRedirect
            flow = DropboxOAuth2FlowNoRedirect(app_key, token_access_type="offline", use_pkce=True, scope=scopes)
        self._flow = flow

    def start(self) -> str:
        return self._flow.start()

    def finish(self, auth_code: str) -> DropboxAuthResult:
        r = self._flow.finish(auth_code.strip())
        return DropboxAuthResult(refresh_token=r.refresh_token, account_id=getattr(r, "account_id", None))


def complete_authorization(app_key: str, auth_code: str, credentials: DropboxCredentialStore, *, flow: Optional[DropboxAuthFlow] = None) -> DropboxAuthResult:
    """Exchange the code and persist ONLY the refresh token; the returned object carries the token for the caller's immediate use and must not be logged."""
    res = (flow or DropboxAuthFlow(app_key)).finish(auth_code)
    credentials.set_refresh_token(res.refresh_token)
    return DropboxAuthResult("", res.account_id)          # the token does not leave this function


def build_client(app_key: str, credentials: DropboxCredentialStore, *, client_factory: Any = None, timeout: float = 100.0) -> Any:
    """SDK client from the stored refresh token (automatic access-token refresh). Raises ``LookupError`` when no credential is stored."""
    token = credentials.get_refresh_token()
    if not token:
        raise LookupError("no Dropbox refresh token stored: run the one-time authorization")
    if client_factory is None:
        import dropbox
        client_factory = dropbox.Dropbox
    return client_factory(oauth2_refresh_token=token, app_key=app_key, timeout=timeout)
