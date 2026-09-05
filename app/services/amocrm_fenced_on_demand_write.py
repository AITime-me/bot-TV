"""One-shot, owner-invoked amoCRM writes.

This is deliberately separate from the API, worker and Chat paths.  It accepts
one narrow plan from stdin, performs a fresh read, optionally applies exactly
one supported change, then rereads it.  There is no queue, scheduler, retry
loop, or egress integration in this module.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from app.core.amocrm_crm_rest_http import AmoCrmCrmRestTransport
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpTransportError

_ALLOWED_LEAD_PATCH = frozenset({"name", "pipeline_id", "status_id", "responsible_user_id", "custom_fields_values"})
_ALLOWED_TASK_PATCH = frozenset({"text", "complete_till", "responsible_user_id", "task_type_id"})
_REQUIRED_TASK_CREATE = frozenset({"entity_type", "text", "complete_till", "responsible_user_id", "task_type_id"})


@dataclass(frozen=True, slots=True)
class FencedWriteReceipt:
    outcome: str
    kind: str | None = None
    entity_id: int | None = None
    error_code: str | None = None


def parse_fenced_write_plan(raw: str) -> dict[str, object]:
    """Parse a single non-secret stdin plan; refuse all unsupported writes."""
    try:
        plan = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("FENCED_WRITE_PLAN_INVALID") from None
    if not isinstance(plan, dict) or set(plan) != {"kind", "entity_id", "patch"}:
        raise ValueError("FENCED_WRITE_PLAN_INVALID")
    kind, entity_id, patch = plan["kind"], plan["entity_id"], plan["patch"]
    if kind not in {"lead_patch", "task_patch", "task_create"}:
        raise ValueError("FENCED_WRITE_KIND_REFUSED")
    if type(entity_id) is not int or isinstance(entity_id, bool) or entity_id <= 0:
        raise ValueError("FENCED_WRITE_ENTITY_ID_INVALID")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("FENCED_WRITE_PATCH_INVALID")
    allowed = _ALLOWED_LEAD_PATCH if kind == "lead_patch" else _ALLOWED_TASK_PATCH
    if kind == "task_create":
        if set(patch) != _REQUIRED_TASK_CREATE or patch.get("entity_type") not in {"leads", "contacts", "customers"}:
            raise ValueError("FENCED_WRITE_FIELD_REFUSED")
    elif not set(patch).issubset(allowed):
        raise ValueError("FENCED_WRITE_FIELD_REFUSED")
    if any(type(key) is not str for key in patch):
        raise ValueError("FENCED_WRITE_PATCH_INVALID")
    # JSON values are re-read and compared exactly. Limit plan size to prevent
    # accidental bulk payloads and keep the disposable operator bounded.
    if len(raw.encode("utf-8")) > 32_768:
        raise ValueError("FENCED_WRITE_PLAN_TOO_LARGE")
    return {"kind": kind, "entity_id": entity_id, "patch": patch}


class FencedOnDemandWriteExecutor:
    """No-background writer: GET → PATCH → GET for one lead or task."""

    def __init__(self, *, api_base_url: str, transport: AmoCrmCrmRestTransport,
                 token_loader: Callable[[], Awaitable[str | None]],
                 refresh_once: Callable[[], Awaitable[bool]]) -> None:
        self._base = api_base_url.rstrip("/")
        self._transport = transport
        self._token_loader = token_loader
        self._refresh_once = refresh_once

    async def execute(self, *, plan: Mapping[str, object], apply: bool) -> FencedWriteReceipt:
        try:
            normalized = parse_fenced_write_plan(json.dumps(dict(plan), ensure_ascii=False, separators=(",", ":")))
            kind = normalized["kind"]
            entity_id = normalized["entity_id"]
            patch = normalized["patch"]
            assert isinstance(kind, str) and isinstance(entity_id, int) and isinstance(patch, dict)
            if kind == "task_create":
                return await self._create_task(entity_id=entity_id, patch=patch, apply=apply)
            path = f"/api/v4/{'leads' if kind == 'lead_patch' else 'tasks'}/{entity_id}"
            before_status, _before = await self._request("GET", path, None)
            if before_status != 200:
                return FencedWriteReceipt("REFUSED", kind, entity_id, "FENCED_WRITE_PRECHECK")
            if not apply:
                return FencedWriteReceipt("DRY_RUN", kind, entity_id)
            status, _ = await self._request("PATCH", path, patch)
            if not 200 <= status < 300:
                return FencedWriteReceipt("FAILED", kind, entity_id, "FENCED_WRITE_PATCH")
            status, after = await self._request("GET", path, None)
            if status != 200 or any(after.get(key) != value for key, value in patch.items()):
                return FencedWriteReceipt("FAILED", kind, entity_id, "FENCED_WRITE_POSTCHECK")
            return FencedWriteReceipt("APPLIED", kind, entity_id)
        except (RuntimeError, ValueError):
            return FencedWriteReceipt("REFUSED", error_code="FENCED_WRITE_REFUSED")

    async def _create_task(self, *, entity_id: int, patch: dict[str, object], apply: bool) -> FencedWriteReceipt:
        path = f"/api/v4/{patch['entity_type']}/{entity_id}"
        status, _ = await self._request("GET", path, None)
        if status != 200:
            return FencedWriteReceipt("REFUSED", "task_create", entity_id, "FENCED_WRITE_PRECHECK")
        if not apply:
            return FencedWriteReceipt("DRY_RUN", "task_create", entity_id)
        body = {"entity_id": entity_id, **patch}
        status, created = await self._request("POST", "/api/v4/tasks", [body])
        task_rows = ((created.get("_embedded") or {}).get("tasks") or [])
        task_id = task_rows[0].get("id") if len(task_rows) == 1 and isinstance(task_rows[0], dict) else None
        if not 200 <= status < 300 or type(task_id) is not int or task_id <= 0:
            return FencedWriteReceipt("FAILED", "task_create", entity_id, "FENCED_WRITE_TASK_CREATE")
        status, after = await self._request("GET", f"/api/v4/tasks/{task_id}", None)
        if status != 200 or after.get("entity_id") != entity_id or any(after.get(key) != value for key, value in patch.items()):
            return FencedWriteReceipt("FAILED", "task_create", task_id, "FENCED_WRITE_POSTCHECK")
        return FencedWriteReceipt("APPLIED", "task_create", task_id)

    async def _request(
        self, method: str, path: str, body: object | None, *, retry_get_401: bool = True
    ) -> tuple[int, dict[str, object]]:
        token = await self._token_loader()
        if not token:
            raise RuntimeError("AMOCRM_CRM_OAUTH_NOT_FOUND")
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        def send(current: str):
            return self._transport.request(S2sHttpRequest(method=method, url=f"{self._base}{path}", headers={"Authorization": f"Bearer {current}", "Content-Type": "application/json"}, body=raw, timeout_seconds=10.0, allow_redirects=False, max_response_bytes=65536))
        try:
            response = send(token)
            if response.status_code == 401 and method == "GET" and retry_get_401:
                if not await self._refresh_once():
                    raise RuntimeError("AMOCRM_CRM_REFRESH_FAILED")
                # One fenced refresh retry for reads only.  Writes are never
                # retried, and a second 401 is returned to the caller.
                return await self._request(method, path, body, retry_get_401=False)
        except S2sHttpTransportError as exc:
            raise RuntimeError("FENCED_WRITE_TRANSPORT") from exc
        try:
            data = json.loads(response.body) if response.body else {}
        except ValueError:
            data = {}
        return response.status_code, data if isinstance(data, dict) else {}
