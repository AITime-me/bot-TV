"""Offline-only fail-closed executor for the approved legacy PROGREV revision.

This is deliberately not a general amoCRM client: its private transport rejects
every method, path and body except the exact read/create/move sequence below.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.amocrm_crm_rest_http import AmoCrmCrmRestTransport
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpTransportError

SOURCE_PIPELINE = 7408150
SOURCE_STATUS = 61561286
TARGET_PIPELINE = 6702678
TARGET_STATUS = 87911490
MANAGER_ID = 9655458
TASK_TYPE_ID = 3176798
TASK_TEXT = "Проверить историю клиента и определить дальнейшее действие"
_LEAD_PATH = re.compile(r"^/api/v4/leads/[1-9][0-9]*$")
_TASKS_PATH = re.compile(
    r"^/api/v4/tasks\?filter\[entity_type\]=leads&filter\[entity_id\]=[1-9][0-9]*&filter\[is_completed\]=0&limit=250$"
)
_TASK_PATH = re.compile(r"^/api/v4/tasks/[1-9][0-9]*$")
_TERMINAL_STATUSES = {142, 143}


@dataclass(frozen=True, slots=True)
class ControlledRevisionReceipt:
    lead_id: int
    outcome: str
    task_id: int | None = None
    error_code: str | None = None


class ControlledRevisionExecutor:
    def __init__(
        self,
        *,
        api_base_url: str,
        transport: AmoCrmCrmRestTransport,
        token_loader: Callable[[], Awaitable[str | None]],
        refresh_once: Callable[[], Awaitable[bool]],
    ) -> None:
        self._base = api_base_url
        self._transport = transport
        self._token_loader = token_loader
        self._refresh_once = refresh_once

    @staticmethod
    def refused(lead_id: int, error_code: str) -> ControlledRevisionReceipt:
        return ControlledRevisionReceipt(lead_id=lead_id, outcome="REFUSED", error_code=error_code)

    @staticmethod
    def _task_path(lead_id: int) -> str:
        return (
            "/api/v4/tasks?filter[entity_type]=leads"
            f"&filter[entity_id]={lead_id}&filter[is_completed]=0&limit=250"
        )

    @staticmethod
    def _single_task_path(task_id: int) -> str:
        return f"/api/v4/tasks/{task_id}"

    @staticmethod
    def _task_body(lead_id: int, complete_till: int) -> list[dict[str, object]]:
        return [{
            "entity_id": lead_id,
            "entity_type": "leads",
            "responsible_user_id": MANAGER_ID,
            "task_type_id": TASK_TYPE_ID,
            "text": TASK_TEXT,
            "complete_till": complete_till,
        }]

    @staticmethod
    def _allowed(method: str, path: str, body: object | None) -> bool:
        if method == "GET":
            return body is None and (_LEAD_PATH.fullmatch(path) is not None or _TASKS_PATH.fullmatch(path) is not None or _TASK_PATH.fullmatch(path) is not None)
        if method == "PATCH":
            if _LEAD_PATH.fullmatch(path) is not None:
                return body == {"pipeline_id": TARGET_PIPELINE, "status_id": TARGET_STATUS} or (isinstance(body, dict) and set(body) == {"pipeline_id", "status_id"} and body.get("pipeline_id") == TARGET_PIPELINE and body.get("status_id") in _TERMINAL_STATUSES)
            return _TASK_PATH.fullmatch(path) is not None and isinstance(body, dict) and set(body) == {"complete_till"} and type(body.get("complete_till")) is int and body["complete_till"] > 0
        if method == "POST" and path == "/api/v4/tasks" and isinstance(body, list) and len(body) == 1:
            row = body[0]
            return isinstance(row, dict) and set(row) == {"entity_id", "entity_type", "responsible_user_id", "task_type_id", "text", "complete_till"} and type(row["entity_id"]) is int and row["entity_id"] > 0 and row["entity_type"] == "leads" and row["responsible_user_id"] == MANAGER_ID and row["task_type_id"] == TASK_TYPE_ID and row["text"] == TASK_TEXT and type(row["complete_till"]) is int and row["complete_till"] > 0
        return False

    async def _request(self, method: str, path: str, body: object | None = None, *, retry_get_401: bool = True) -> tuple[int, dict]:
        if not self._allowed(method, path, body):
            raise ValueError("CONTROLLED_REVISION_REQUEST_REFUSED")
        token = await self._token_loader()
        if not token:
            raise RuntimeError("AMOCRM_CRM_OAUTH_NOT_FOUND")
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        def send(current_token: str):
            return self._transport.request(S2sHttpRequest(method=method, url=f"{self._base}{path}", headers={"Authorization": f"Bearer {current_token}", "Content-Type": "application/json"}, body=raw, timeout_seconds=10.0, allow_redirects=False, max_response_bytes=65536))
        try:
            response = send(token)
            if response.status_code == 401 and method == "GET" and retry_get_401:
                if not await self._refresh_once():
                    raise RuntimeError("AMOCRM_CRM_REFRESH_FAILED")
                return await self._request(method, path, body, retry_get_401=False)
        except S2sHttpTransportError as exc:
            raise RuntimeError("CONTROLLED_REVISION_TRANSPORT") from exc
        try:
            return response.status_code, json.loads(response.body) if response.body else {}
        except ValueError:
            return response.status_code, {}

    async def _lead(self, lead_id: int) -> tuple[int, dict]:
        return await self._request("GET", f"/api/v4/leads/{lead_id}")

    async def _active_tasks(self, lead_id: int) -> list[dict]:
        status, data = await self._request("GET", self._task_path(lead_id))
        # amoCRM returns 204 No Content for a valid targeted query with no tasks.
        # That is an unambiguous empty set; every other non-200 status remains
        # fail-closed below.
        if status == 204:
            return []
        rows = (data.get("_embedded") or {}).get("tasks") or []
        if status != 200 or not isinstance(rows, list) or (data.get("_links") or {}).get("next"):
            raise RuntimeError("CONTROLLED_REVISION_TASK_CHECK_FAILED")
        if any(not isinstance(row, dict) or row.get("entity_type") != "leads" or row.get("entity_id") != lead_id for row in rows):
            raise RuntimeError("CONTROLLED_REVISION_TASK_ENTITY_MISMATCH")
        return rows

    async def _task(self, task_id: int) -> tuple[int, dict]:
        return await self._request("GET", self._single_task_path(task_id))

    async def execute(self, *, lead_id: int, complete_till: int, apply: bool) -> ControlledRevisionReceipt:
        if type(lead_id) is not int or lead_id <= 0 or type(complete_till) is not int or complete_till <= 0:
            return self.refused(lead_id, "CONTROLLED_REVISION_INPUT_INVALID")
        try:
            status, lead = await self._lead(lead_id)
            if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or lead.get("status_id") != SOURCE_STATUS:
                return self.refused(lead_id, "CONTROLLED_REVISION_SOURCE_STATE")
            if await self._active_tasks(lead_id):
                return self.refused(lead_id, "CONTROLLED_REVISION_ACTIVE_TASK")
            if not apply:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="DRY_RUN")
            status, task_data = await self._request("POST", "/api/v4/tasks", self._task_body(lead_id, complete_till))
            if not 200 <= status < 300:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_REVISION_TASK_CREATE")
            task_id = (((task_data.get("_embedded") or {}).get("tasks") or [{}])[0]).get("id")
            status, lead = await self._lead(lead_id)
            if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or lead.get("status_id") != SOURCE_STATUS:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_STATE_CHANGED")
            status, _ = await self._request("PATCH", f"/api/v4/leads/{lead_id}", {"pipeline_id": TARGET_PIPELINE, "status_id": TARGET_STATUS})
            if not 200 <= status < 300:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_MOVE")
            status, lead = await self._lead(lead_id)
            matching = [row for row in await self._active_tasks(lead_id) if row.get("responsible_user_id") == MANAGER_ID and row.get("task_type_id") == TASK_TYPE_ID and row.get("text") == TASK_TEXT and row.get("complete_till") == complete_till]
            if status != 200 or lead.get("pipeline_id") != TARGET_PIPELINE or lead.get("status_id") != TARGET_STATUS or len(matching) != 1:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_POSTCHECK")
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="APPLIED", task_id=task_id)
        except (RuntimeError, ValueError) as exc:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code=str(exc))

    async def execute_move_only(self, *, lead_id: int, apply: bool) -> ControlledRevisionReceipt:
        """Move an open PROGREV lead only when its active tasks remain unchanged."""
        if type(lead_id) is not int or lead_id <= 0:
            return self.refused(lead_id, "CONTROLLED_REVISION_INPUT_INVALID")
        try:
            status, lead = await self._lead(lead_id)
            if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or lead.get("status_id") != SOURCE_STATUS:
                return self.refused(lead_id, "CONTROLLED_MOVE_ONLY_SOURCE_STATE")
            before = await self._active_tasks(lead_id)
            if not before:
                return self.refused(lead_id, "CONTROLLED_MOVE_ONLY_NO_ACTIVE_TASK")
            if not apply:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="DRY_RUN")
            status, _ = await self._request("PATCH", f"/api/v4/leads/{lead_id}", {"pipeline_id": TARGET_PIPELINE, "status_id": TARGET_STATUS})
            if not 200 <= status < 300:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_MOVE_ONLY_MOVE")
            status, lead = await self._lead(lead_id)
            after = await self._active_tasks(lead_id)
            if status != 200 or lead.get("pipeline_id") != TARGET_PIPELINE or lead.get("status_id") != TARGET_STATUS or after != before:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_MOVE_ONLY_POSTCHECK")
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="APPLIED")
        except (RuntimeError, ValueError) as exc:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code=str(exc))

    async def execute_reschedule_control_task(self, *, task_id: int, complete_till: int, apply: bool) -> ControlledRevisionReceipt:
        if type(task_id) is not int or task_id <= 0 or type(complete_till) is not int or complete_till <= 0:
            return self.refused(task_id, "CONTROLLED_REBALANCE_INPUT_INVALID")
        try:
            status, before = await self._task(task_id)
            fixed = before.get("entity_type") == "leads" and before.get("responsible_user_id") == MANAGER_ID and before.get("task_type_id") == TASK_TYPE_ID and before.get("text") == TASK_TEXT and not before.get("is_completed")
            if status != 200 or not fixed:
                return self.refused(task_id, "CONTROLLED_REBALANCE_TASK_SCOPE")
            if not apply:
                return ControlledRevisionReceipt(lead_id=before.get("entity_id", task_id), task_id=task_id, outcome="DRY_RUN")
            status, _ = await self._request("PATCH", self._single_task_path(task_id), {"complete_till": complete_till})
            if not 200 <= status < 300:
                return ControlledRevisionReceipt(lead_id=before.get("entity_id", task_id), task_id=task_id, outcome="FAILED", error_code="CONTROLLED_REBALANCE_PATCH")
            status, after = await self._task(task_id)
            unchanged = all(after.get(key) == before.get(key) for key in ("entity_id", "entity_type", "responsible_user_id", "task_type_id", "text", "is_completed"))
            if status != 200 or after.get("complete_till") != complete_till or not unchanged:
                return ControlledRevisionReceipt(lead_id=before.get("entity_id", task_id), task_id=task_id, outcome="FAILED", error_code="CONTROLLED_REBALANCE_POSTCHECK")
            return ControlledRevisionReceipt(lead_id=before.get("entity_id", task_id), task_id=task_id, outcome="APPLIED")
        except (RuntimeError, ValueError) as exc:
            return ControlledRevisionReceipt(lead_id=task_id, task_id=task_id, outcome="FAILED", error_code=str(exc))

    async def execute_terminal_move(self, *, lead_id: int, apply: bool) -> ControlledRevisionReceipt:
        if type(lead_id) is not int or lead_id <= 0:
            return self.refused(lead_id, "CONTROLLED_TERMINAL_INPUT_INVALID")
        try:
            status, lead = await self._lead(lead_id)
            terminal = lead.get("status_id") in _TERMINAL_STATUSES
            if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or not terminal:
                return self.refused(lead_id, "CONTROLLED_TERMINAL_SOURCE_STATE")
            before = await self._active_tasks(lead_id)
            if not apply:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="DRY_RUN")
            target_status = lead["status_id"]
            status, _ = await self._request("PATCH", f"/api/v4/leads/{lead_id}", {"pipeline_id": TARGET_PIPELINE, "status_id": target_status})
            if not 200 <= status < 300:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_TERMINAL_MOVE")
            status, after = await self._lead(lead_id)
            tasks_after = await self._active_tasks(lead_id)
            if status != 200 or after.get("pipeline_id") != TARGET_PIPELINE or after.get("status_id") != target_status or tasks_after != before:
                return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_TERMINAL_POSTCHECK")
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="APPLIED")
        except (RuntimeError, ValueError) as exc:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code=str(exc))
