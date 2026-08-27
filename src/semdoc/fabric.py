"""Fabric and Power BI REST client.

Deliberately pure HTTP (design decision D2). Everything the documentation generator
needs is reachable without XMLA, ADOMD.NET, or the .NET runtime:

- `get_model_definition` -> the complete model structure as TMSL (`model.bim`, JSON)
- `execute_dax`          -> DAX execution, used for `INFO.*` stats and for verifying
                            generated DAX snippets actually run

The tradeoff is that `executeQueries` allows one query per call and caps result rows, so
this client is not suitable for bulk data extraction. We only ever pull metadata and
validation results through it.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx

from semdoc.auth import FABRIC_SCOPE, POWERBI_SCOPE, Credential

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"

# `getDefinition` is a long-running operation. These bound the polling loop.
LRO_POLL_SECONDS = 2.0
LRO_TIMEOUT_SECONDS = 300.0


class FabricError(RuntimeError):
    pass


class NotFoundError(FabricError):
    pass


class FabricClient:
    def __init__(self, credential: Credential, timeout: float = 120.0):
        self._cred = credential
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> FabricClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- plumbing ----------------------------------------------------------------------

    def _headers(self, scope: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._cred.token(scope)}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        scope: str = FABRIC_SCOPE,
        json_body: Any = None,
    ) -> httpx.Response:
        resp = self._http.request(method, url, headers=self._headers(scope), json=json_body)
        if resp.status_code >= 400:
            raise FabricError(f"{method} {url} -> {resp.status_code}: {resp.text[:800]}")
        return resp

    def _get_paged(self, url: str, *, scope: str = FABRIC_SCOPE) -> list[dict]:
        """Follow Fabric's continuation-token pagination to completion."""
        items: list[dict] = []
        next_url: str | None = url
        while next_url:
            payload = self._request("GET", next_url, scope=scope).json()
            items.extend(payload.get("value", []))
            next_url = payload.get("continuationUri")
        return items

    def _await_lro(self, resp: httpx.Response) -> dict:
        """Resolve a 202 long-running operation to its result payload."""
        if resp.status_code != 202:
            return resp.json()

        operation_id = resp.headers.get("x-ms-operation-id")
        location = resp.headers.get("Location")
        status_url = location or f"{FABRIC_API}/operations/{operation_id}"
        if not (location or operation_id):
            raise FabricError("Got HTTP 202 but no Location or x-ms-operation-id to poll.")

        deadline = time.monotonic() + LRO_TIMEOUT_SECONDS
        while True:
            status = self._request("GET", status_url).json()
            state = (status.get("status") or "").lower()

            if state == "succeeded":
                break
            if state in {"failed", "cancelled"}:
                raise FabricError(f"Operation {state}: {status.get('error')}")
            if time.monotonic() > deadline:
                raise FabricError(
                    f"Operation did not complete within {LRO_TIMEOUT_SECONDS:.0f}s "
                    f"(last status: {state or 'unknown'})."
                )
            time.sleep(LRO_POLL_SECONDS)

        result_url = f"{FABRIC_API}/operations/{operation_id}/result"
        return self._request("GET", result_url).json()

    # -- discovery ---------------------------------------------------------------------

    def list_workspaces(self) -> list[dict]:
        return self._get_paged(f"{FABRIC_API}/workspaces")

    def find_workspace(self, name: str) -> dict:
        workspaces = self.list_workspaces()
        match = next((w for w in workspaces if w.get("displayName") == name), None)
        if match:
            return match

        # Case-insensitive second pass, since workspace names are easy to mistype.
        lowered = name.casefold()
        match = next((w for w in workspaces if (w.get("displayName") or "").casefold() == lowered), None)
        if match:
            return match

        available = ", ".join(sorted(w.get("displayName", "?") for w in workspaces)) or "none visible"
        raise NotFoundError(f"No workspace named {name!r}. Available: {available}")

    def list_semantic_models(self, workspace_id: str) -> list[dict]:
        return self._get_paged(f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels")

    def get_item(self, workspace_id: str, item_id: str) -> dict:
        return self._request("GET", f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}").json()

    def get_warehouse(self, workspace_id: str, warehouse_id: str) -> dict:
        return self._request(
            "GET", f"{FABRIC_API}/workspaces/{workspace_id}/warehouses/{warehouse_id}"
        ).json()

    def get_lakehouse(self, workspace_id: str, lakehouse_id: str) -> dict:
        return self._request(
            "GET", f"{FABRIC_API}/workspaces/{workspace_id}/lakehouses/{lakehouse_id}"
        ).json()

    def resolve_sql_endpoint(self, workspace_id: str, item_id: str) -> tuple[str, str] | None:
        """Resolve a workspace/item id pair to its SQL analytics endpoint (server, database).

        DirectLake models reference OneLake directly by workspace/item GUID rather than a
        connection string — this is how those GUIDs turn into something `semdoc.warehouse`
        can actually connect to. Supports the two item types DirectLake can sit on
        (Warehouse and Lakehouse); each exposes the connection string under a different
        JSON path. Returns None for any other item type, or if no SQL endpoint has been
        provisioned yet.
        """
        item = self.get_item(workspace_id, item_id)
        item_type = item.get("type")
        display_name = item.get("displayName", "")

        if item_type == "Warehouse":
            details = self.get_warehouse(workspace_id, item_id)
            server = details.get("properties", {}).get("connectionString")
        elif item_type == "Lakehouse":
            details = self.get_lakehouse(workspace_id, item_id)
            server = (details.get("properties") or {}).get("sqlEndpointProperties", {}).get(
                "connectionString"
            )
        else:
            return None

        return (server, display_name) if server else None

    def find_semantic_model(self, workspace_id: str, name: str) -> dict:
        models = self.list_semantic_models(workspace_id)
        lowered = name.casefold()
        match = next(
            (m for m in models if (m.get("displayName") or "").casefold() == lowered), None
        )
        if match:
            return match

        available = ", ".join(sorted(m.get("displayName", "?") for m in models)) or "none"
        raise NotFoundError(f"No semantic model named {name!r} in workspace. Available: {available}")

    # -- extraction --------------------------------------------------------------------

    def get_model_definition(self, workspace_id: str, model_id: str, fmt: str = "TMSL") -> dict[str, str]:
        """Return the model definition parts as {path: decoded text}.

        `fmt="TMSL"` yields a single `model.bim` JSON part, which is what the extractor
        parses. `fmt="TMDL"` yields a collection of readable .tmdl files, useful only for
        showing humans a snippet.
        """
        url = (
            f"{FABRIC_API}/workspaces/{workspace_id}/semanticModels/{model_id}"
            f"/getDefinition?format={fmt}"
        )
        payload = self._await_lro(self._request("POST", url))

        parts: dict[str, str] = {}
        for part in payload.get("definition", {}).get("parts", []):
            path = part.get("path", "")
            encoded = part.get("payload", "")
            if part.get("payloadType") != "InlineBase64":
                continue
            parts[path] = base64.b64decode(encoded).decode("utf-8")

        if not parts:
            raise FabricError(f"getDefinition returned no usable parts for model {model_id}.")
        return parts

    def get_tmsl(self, workspace_id: str, model_id: str) -> dict:
        """Return the parsed `model.bim` TMSL document."""
        parts = self.get_model_definition(workspace_id, model_id, fmt="TMSL")
        for path, text in parts.items():
            if path.casefold().endswith(".bim"):
                return json.loads(text)
        raise FabricError(f"No .bim part in definition. Got parts: {list(parts)}")

    def execute_dax(self, workspace_id: str, dataset_id: str, dax: str) -> list[dict]:
        """Run one DAX query and return its rows.

        Used for `INFO.*` metadata queries, cardinality stats, and — importantly — to
        verify that DAX we generated actually executes against the real model.
        """
        url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        body = {
            "queries": [{"query": dax}],
            "serializerSettings": {"includeNulls": True},
        }
        payload = self._request("POST", url, scope=POWERBI_SCOPE, json_body=body).json()
        results = payload.get("results", [])
        if not results:
            return []
        tables = results[0].get("tables", [])
        return tables[0].get("rows", []) if tables else []

    def try_execute_dax(self, workspace_id: str, dataset_id: str, dax: str) -> tuple[bool, str | None]:
        """Execute DAX for validation purposes, returning (ok, error message)."""
        try:
            self.execute_dax(workspace_id, dataset_id, dax)
            return True, None
        except FabricError as exc:
            return False, str(exc)
