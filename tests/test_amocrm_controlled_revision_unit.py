"""Fail-closed coverage for the disposable controlled-revision executor."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_controlled_revision import (
    MANAGER_ID,
    SOURCE_PIPELINE,
    SOURCE_STATUS,
    TARGET_PIPELINE,
    TARGET_STATUS,
    TASK_TEXT,
    TASK_TYPE_ID,
    ControlledRevisionExecutor,
)

_REPO = Path(__file__).resolve().parents[1]
_LEAD_ID = 42
_DUE = 1786993140


class _Transport:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.calls: list[S2sHttpRequest] = []
        self.responses = list(responses)

    def request(self, req: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(req)
        status, body = self.responses.pop(0)
        return S2sHttpResponse(status_code=status, headers={}, body=json.dumps(body).encode())


def _lead(pipeline: int = SOURCE_PIPELINE, status: int = SOURCE_STATUS) -> dict:
    return {"id": _LEAD_ID, "pipeline_id": pipeline, "status_id": status}


def _tasks(*rows: dict) -> dict:
    return {"_embedded": {"tasks": list(rows)}}


def _task() -> dict:
    return {"id": 700, "entity_id": _LEAD_ID, "entity_type": "leads", "responsible_user_id": MANAGER_ID, "task_type_id": TASK_TYPE_ID, "text": TASK_TEXT, "complete_till": _DUE}


def _executor(transport: _Transport, *, refreshed: list[bool] | None = None) -> ControlledRevisionExecutor:
    async def token() -> str | None:
        return "fresh-token"

    async def refresh() -> bool:
        if refreshed is not None:
            refreshed.append(True)
        return True

    return ControlledRevisionExecutor(api_base_url="https://example.amocrm.ru", transport=transport, token_loader=token, refresh_once=refresh)


@pytest.mark.asyncio
async def test_dry_run_is_zero_writes() -> None:
    transport = _Transport([(200, _lead()), (200, _tasks())])
    receipt = await _executor(transport).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=False)
    assert receipt.outcome == "DRY_RUN"
    assert [call.method for call in transport.calls] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_wrong_source_and_existing_task_are_zero_writes() -> None:
    wrong = _Transport([(200, _lead(status=1))])
    assert (await _executor(wrong).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=True)).outcome == "REFUSED"
    active = _Transport([(200, _lead()), (200, _tasks(_task()))])
    assert (await _executor(active).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=True)).error_code == "CONTROLLED_REVISION_ACTIVE_TASK"
    assert all(call.method == "GET" for call in wrong.calls + active.calls)


@pytest.mark.asyncio
async def test_apply_uses_exact_post_and_patch_only() -> None:
    transport = _Transport([(200, _lead()), (200, _tasks()), (201, {"_embedded": {"tasks": [{"id": 700}]}}), (200, _lead()), (200, {}), (200, _lead(TARGET_PIPELINE, TARGET_STATUS)), (200, _tasks(_task()))])
    receipt = await _executor(transport).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=True)
    assert receipt.outcome == "APPLIED"
    post, patch = [call for call in transport.calls if call.method in {"POST", "PATCH"}]
    assert post.url.endswith("/api/v4/tasks")
    assert json.loads(post.body) == [{"entity_id": _LEAD_ID, "entity_type": "leads", "responsible_user_id": MANAGER_ID, "task_type_id": TASK_TYPE_ID, "text": TASK_TEXT, "complete_till": _DUE}]
    assert json.loads(patch.body) == {"pipeline_id": TARGET_PIPELINE, "status_id": TARGET_STATUS}


@pytest.mark.asyncio
async def test_get_401_refreshes_once_reloads_and_retries_get() -> None:
    transport = _Transport([(401, {}), (200, _lead()), (200, _tasks())])
    refreshed: list[bool] = []
    receipt = await _executor(transport, refreshed=refreshed).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=False)
    assert receipt.outcome == "DRY_RUN"
    assert refreshed == [True]
    assert [call.method for call in transport.calls] == ["GET", "GET", "GET"]


@pytest.mark.asyncio
async def test_postcheck_and_entity_mismatch_fail_closed() -> None:
    postcheck = _Transport([(200, _lead()), (200, _tasks()), (201, {"_embedded": {"tasks": [{"id": 700}]}}), (200, _lead()), (200, {}), (200, _lead(TARGET_PIPELINE, TARGET_STATUS)), (200, _tasks())])
    assert (await _executor(postcheck).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=True)).error_code == "CONTROLLED_REVISION_POSTCHECK"
    mismatch = _Transport([(200, _lead()), (200, _tasks({"entity_id": _LEAD_ID, "entity_type": "contacts"}))])
    assert (await _executor(mismatch).execute(lead_id=_LEAD_ID, complete_till=_DUE, apply=False)).error_code == "CONTROLLED_REVISION_TASK_ENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_other_endpoint_or_body_is_refused_before_transport() -> None:
    transport = _Transport([])
    executor = _executor(transport)
    with pytest.raises(ValueError, match="REQUEST_REFUSED"):
        await executor._request("PATCH", "/api/v4/leads/42", {"name": "forbidden"})
    with pytest.raises(ValueError, match="REQUEST_REFUSED"):
        await executor._request("POST", "/api/v4/leads", [])
    assert transport.calls == []


def test_api_worker_and_default_image_cannot_activate_executor() -> None:
    default_ignore = (_REPO / ".dockerignore").read_text(encoding="utf-8")
    assert "!app/amocrm_crm_ops.py" not in default_ignore
    assert "!app/services/amocrm_crm_ops.py" not in default_ignore
    assert "!app/services/amocrm_controlled_revision.py" not in default_ignore
    for rel in ("app/main.py", "app/worker.py", "app/services/worker_runtime.py"):
        assert "amocrm_controlled_revision" not in (_REPO / rel).read_text(encoding="utf-8")
    compose = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'profiles: ["controlled-revision"]' in compose
    assert 'restart: "no"' in compose
    assert "Dockerfile.controlled-revision" in compose
    assert "COPY app ./app" in (_REPO / "Dockerfile.controlled-revision").read_text(encoding="utf-8")
    assert "!app/**" in (_REPO / "Dockerfile.controlled-revision.dockerignore").read_text(encoding="utf-8")
