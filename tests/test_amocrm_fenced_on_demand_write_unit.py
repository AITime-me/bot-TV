from __future__ import annotations

import json

import pytest

from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.services.amocrm_fenced_on_demand_write import FencedOnDemandWriteExecutor, parse_fenced_write_plan


class _Transport:
    def __init__(self, responses: list[tuple[int, dict]]) -> None:
        self.responses = list(responses)
        self.calls: list[S2sHttpRequest] = []
    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        status, body = self.responses.pop(0)
        return S2sHttpResponse(status_code=status, headers={}, body=json.dumps(body).encode())


def test_plan_refuses_create_delete_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="KIND_REFUSED"):
        parse_fenced_write_plan('{"kind":"lead_create","entity_id":1,"patch":{"name":"x"}}')
    with pytest.raises(ValueError, match="FIELD_REFUSED"):
        parse_fenced_write_plan('{"kind":"lead_patch","entity_id":1,"patch":{"delete":true}}')


@pytest.mark.asyncio
async def test_dry_run_is_fresh_read_only() -> None:
    transport = _Transport([(200, {"id": 42, "status_id": 1})])
    async def token() -> str | None: return "token"
    async def refresh() -> bool: return True
    receipt = await FencedOnDemandWriteExecutor(api_base_url="https://example.amocrm.ru", transport=transport, token_loader=token, refresh_once=refresh).execute(plan={"kind":"lead_patch","entity_id":42,"patch":{"status_id":2}}, apply=False)
    assert receipt.outcome == "DRY_RUN"
    assert [call.method for call in transport.calls] == ["GET"]


@pytest.mark.asyncio
async def test_apply_is_only_get_patch_get_and_postchecks_patch() -> None:
    transport = _Transport([(200, {"id": 42, "status_id": 1}), (200, {}), (200, {"id": 42, "status_id": 2})])
    async def token() -> str | None: return "token"
    async def refresh() -> bool: return True
    receipt = await FencedOnDemandWriteExecutor(api_base_url="https://example.amocrm.ru", transport=transport, token_loader=token, refresh_once=refresh).execute(plan={"kind":"lead_patch","entity_id":42,"patch":{"status_id":2}}, apply=True)
    assert receipt.outcome == "APPLIED"
    assert [call.method for call in transport.calls] == ["GET", "PATCH", "GET"]
    assert json.loads(transport.calls[1].body) == {"status_id": 2}
    assert all("chat" not in call.url for call in transport.calls)


@pytest.mark.asyncio
async def test_task_create_is_entity_read_post_task_read() -> None:
    patch = {"entity_type":"leads", "text":"Technical proof", "complete_till":1786993140, "responsible_user_id":1, "task_type_id":1}
    transport = _Transport([(200, {"id": 42}), (200, {"_embedded":{"tasks":[{"id":700}]}}), (200, {"id":700, "entity_id":42, **patch})])
    async def token() -> str | None: return "token"
    async def refresh() -> bool: return True
    receipt = await FencedOnDemandWriteExecutor(api_base_url="https://example.amocrm.ru", transport=transport, token_loader=token, refresh_once=refresh).execute(plan={"kind":"task_create","entity_id":42,"patch":patch}, apply=True)
    assert receipt.outcome == "APPLIED"
    assert [call.method for call in transport.calls] == ["GET", "POST", "GET"]
    assert json.loads(transport.calls[1].body) == [{"entity_id":42, **patch}]
