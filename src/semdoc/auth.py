"""Authentication to Fabric and Power BI REST APIs.

Two implementations behind one protocol (design decision D4):

- `InteractiveCredential` signs in as you. No tenant admin involvement, so it unblocks
  the POC immediately, but it cannot be automated.
- `ServicePrincipalCredential` is the automation path. It additionally requires the
  tenant setting "Service principals can use Fabric APIs" and workspace access for the
  app registration, which usually needs an admin.

Callers take a `Credential` and never care which one they got.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Protocol

import msal

# Fabric and Power BI are distinct token audiences. `getDefinition` and other Fabric
# item APIs use the Fabric audience; `executeQueries` (which we use for DAX-based stats
# and for verifying generated DAX) is a Power BI API and needs the Power BI audience.
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Microsoft Azure PowerShell public client. Pre-consented in most tenants, which means
# interactive sign-in works without registering an app first. Override with
# SEMDOC_CLIENT_ID if your tenant blocks it.
DEFAULT_PUBLIC_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"

CACHE_DIR = pathlib.Path.home() / ".semdoc"
CACHE_PATH = CACHE_DIR / "token_cache.json"


class AuthError(RuntimeError):
    pass


class Credential(Protocol):
    """Anything that can hand out a bearer token for a given audience."""

    def token(self, scope: str) -> str: ...


class InteractiveCredential:
    """Signs in as the current user, caching tokens between runs.

    The cache holds a refresh token in plaintext under ~/.semdoc, in the same spirit as
    the Azure CLI's own cache. It is outside the repo and gitignored, but treat the file
    as a credential: deleting it simply forces a fresh sign-in.
    """

    def __init__(self, tenant_id: str | None = None, client_id: str | None = None):
        self.tenant_id = tenant_id or os.environ.get("SEMDOC_TENANT_ID") or "organizations"
        self.client_id = client_id or os.environ.get("SEMDOC_CLIENT_ID") or DEFAULT_PUBLIC_CLIENT_ID

        self._cache = msal.SerializableTokenCache()
        if CACHE_PATH.exists():
            self._cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))

        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=self._cache,
        )

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(self._cache.serialize(), encoding="utf-8")
        try:
            CACHE_PATH.chmod(0o600)
        except OSError:
            # Best effort; Windows ACLs do not map onto POSIX modes.
            pass

    def token(self, scope: str) -> str:
        scopes = [scope]
        accounts = self._app.get_accounts()
        result = None

        if accounts:
            result = self._app.acquire_token_silent(scopes, account=accounts[0])

        if not result:
            result = self._acquire_new(scopes)

        self._save_cache()

        if "access_token" not in result:
            raise AuthError(
                f"Sign-in failed: {result.get('error')}: {result.get('error_description')}"
            )
        return result["access_token"]

    def _acquire_new(self, scopes: list[str]) -> dict:
        # Prefer the browser flow; fall back to device code for headless shells or when
        # no system browser is reachable.
        try:
            result = self._app.acquire_token_interactive(scopes, prompt="select_account")
            if "access_token" in result:
                return result
        except Exception:
            pass

        flow = self._app.initiate_device_flow(scopes)
        if "user_code" not in flow:
            raise AuthError(f"Could not start device code flow: {flow}")
        print(flow["message"], file=sys.stderr, flush=True)
        return self._app.acquire_token_by_device_flow(flow)


class ServicePrincipalCredential:
    """App-only auth for automation."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self.tenant_id = tenant_id
        self._app = msal.ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )

    def token(self, scope: str) -> str:
        result = self._app.acquire_token_for_client(scopes=[scope])
        if "access_token" not in result:
            raise AuthError(
                f"Service principal auth failed: {result.get('error')}: "
                f"{result.get('error_description')}"
            )
        return result["access_token"]


def credential_from_env() -> Credential:
    """Pick a credential based on what the environment provides.

    A client secret means the service principal path; otherwise sign in interactively.
    """
    tenant_id = os.environ.get("SEMDOC_TENANT_ID")
    client_id = os.environ.get("SEMDOC_CLIENT_ID")
    client_secret = os.environ.get("SEMDOC_CLIENT_SECRET")

    if client_secret:
        if not (tenant_id and client_id):
            raise AuthError(
                "SEMDOC_CLIENT_SECRET is set, so service principal auth was selected, "
                "but SEMDOC_TENANT_ID and SEMDOC_CLIENT_ID are also required."
            )
        return ServicePrincipalCredential(tenant_id, client_id, client_secret)

    return InteractiveCredential(tenant_id=tenant_id, client_id=client_id)
