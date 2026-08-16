"""Fail-closed executor for the owner-approved legacy PROGREV revision.

This module deliberately exposes no generic CRM write API.  It can only create
the one approved task and move a lead from the one approved source state to the
one approved target state, after fresh checks and with a post-write receipt.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.amocrm_crm_rest_http import _CrmHttpStdlibTransport
from app.core.s2s_http_transport import S2sHttpRequest

SOURCE_PIPELINE = 7408150
SOURCE_STATUS = 61561286
TARGET_PIPELINE = 6702678
TARGET_STATUS = 87911490
MANAGER_ID = 9655458
TASK_TYPE_ID = 3176798
TASK_TEXT = "Проверить историю клиента и определить дальнейшее действие"


@dataclass(frozen=True, slots=True)
class ControlledRevisionReceipt:
    lead_id: int
    outcome: str
    task_id: int | None = None
    error_code: str | None = None


class ControlledRevisionExecutor:
    def __init__(self, *, api_base_url: str, access_token: str) -> None:
        self._base = api_base_url
        self._token = access_token
        self._transport = _CrmHttpStdlibTransport()

    def _request(self, method: str, path: str, body: object | None = None) -> tuple[int, dict]:
        if method not in {"GET", "POST", "PATCH"} or not path.startswith("/api/v4/"):
            raise ValueError("CONTROLLED_REVISION_REQUEST_REFUSED")
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        response = self._transport.request(S2sHttpRequest(method=method, url=f"{self._base}{path}", headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}, body=raw, timeout_seconds=10.0, allow_redirects=False, max_response_bytes=65536))
        try:
            payload = json.loads(response.body) if response.body else {}
        except ValueError:
            payload = {}
        return response.status_code, payload

    def _active_tasks(self) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 61):
            status, data = self._request("GET", f"/api/v4/tasks?filter[entity_type]=leads&filter[is_completed]=0&limit=50&page={page}")
            if status != 200:
                raise RuntimeError("CONTROLLED_REVISION_TASK_CHECK_FAILED")
            page_rows = (data.get("_embedded") or {}).get("tasks") or []
            rows.extend(page_rows)
            if len(page_rows) < 50:
                return rows
        raise RuntimeError("CONTROLLED_REVISION_TASK_PAGE_CAP")

    def execute(self, *, lead_id: int, complete_till: int, apply: bool) -> ControlledRevisionReceipt:
        if type(lead_id) is not int or lead_id <= 0 or type(complete_till) is not int or complete_till <= 0:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="REFUSED", error_code="CONTROLLED_REVISION_INPUT_INVALID")
        status, lead = self._request("GET", f"/api/v4/leads/{lead_id}")
        if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or lead.get("status_id") != SOURCE_STATUS:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="REFUSED", error_code="CONTROLLED_REVISION_SOURCE_STATE")
        if any(task.get("entity_id") == lead_id for task in self._active_tasks()):
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="REFUSED", error_code="CONTROLLED_REVISION_ACTIVE_TASK")
        if not apply:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="DRY_RUN")
        status, task_data = self._request("POST", "/api/v4/tasks", [{"entity_id": lead_id, "entity_type": "leads", "responsible_user_id": MANAGER_ID, "task_type_id": TASK_TYPE_ID, "text": TASK_TEXT, "complete_till": complete_till}])
        if not 200 <= status < 300:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", error_code="CONTROLLED_REVISION_TASK_CREATE")
        task_id = (((task_data.get("_embedded") or {}).get("tasks") or [{}])[0]).get("id")
        status, lead = self._request("GET", f"/api/v4/leads/{lead_id}")
        if status != 200 or lead.get("pipeline_id") != SOURCE_PIPELINE or lead.get("status_id") != SOURCE_STATUS:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_STATE_CHANGED")
        status, _ = self._request("PATCH", f"/api/v4/leads/{lead_id}", {"pipeline_id": TARGET_PIPELINE, "status_id": TARGET_STATUS})
        if not 200 <= status < 300:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_MOVE")
        status, lead = self._request("GET", f"/api/v4/leads/{lead_id}")
        matching = [t for t in self._active_tasks() if t.get("entity_id") == lead_id and t.get("responsible_user_id") == MANAGER_ID and t.get("task_type_id") == TASK_TYPE_ID and t.get("text") == TASK_TEXT and t.get("complete_till") == complete_till]
        if status != 200 or lead.get("pipeline_id") != TARGET_PIPELINE or lead.get("status_id") != TARGET_STATUS or len(matching) != 1:
            return ControlledRevisionReceipt(lead_id=lead_id, outcome="FAILED", task_id=task_id, error_code="CONTROLLED_REVISION_POSTCHECK")
        return ControlledRevisionReceipt(lead_id=lead_id, outcome="APPLIED", task_id=task_id)
