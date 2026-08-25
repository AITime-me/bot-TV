"""Minimal read-only Teya request ops visibility (no PII)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.amocrm_circuit_breaker import AMOCRM_BUSINESS_WRITES_BREAKER_KEY
from app.db.session import session_scope
from app.models.teya_request_pending import TeyaRequestPending
from app.repositories import integration_circuit_breakers as breaker_repo


class TeyaOpsConfig:
    def __init__(self, token: str) -> None:
        self._token = token

    def verify_token(self, provided: str | None) -> bool:
        return type(provided) is str and provided != "" and provided == self._token


class PendingOpsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_id: str
    state: str
    result_code: str | None = None
    manual_review_reason: str | None = None
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None = None
    updated_at: datetime
    amocrm_contact_id: str | None = None
    amocrm_deal_id: str | None = None
    amocrm_task_id: str | None = None


class BreakerOpsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    state: str
    failure_count: int
    opened_at: datetime | None = None
    updated_at: datetime


class TeyaOpsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pendings: list[PendingOpsItem] = Field(default_factory=list)
    breaker: BreakerOpsItem | None = None


def load_teya_ops_config(
    env: dict[str, str] | None = None,
) -> TeyaOpsConfig | None:
    source = env if env is not None else dict(os.environ)
    token = source.get("TEYA_OPS_TOKEN", "").strip()
    if len(token) < 16:
        return None
    return TeyaOpsConfig(token)


def build_teya_ops_router(
    *,
    config: TeyaOpsConfig,
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    router = APIRouter(prefix="/internal/teya-ops", tags=["teya-ops"])

    def _authorize(
        x_teya_ops_token: Annotated[
            str | None, Header(alias="X-Teya-Ops-Token")
        ] = None,
    ) -> None:
        if not config.verify_token(x_teya_ops_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="UNAUTHORIZED",
            )

    @router.get("/snapshot", response_model=TeyaOpsSnapshot)
    async def snapshot(_: None = Depends(_authorize)) -> TeyaOpsSnapshot:
        async with session_scope(session_factory) as session:
            rows = (
                await session.scalars(
                    select(TeyaRequestPending)
                    .order_by(TeyaRequestPending.updated_at.desc())
                    .limit(100)
                )
            ).all()
            from app.db.clock import db_statement_now

            now = await db_statement_now(session)
            breaker = await breaker_repo.get(session)
            breaker_item: BreakerOpsItem | None
            if breaker is None:
                # Semantic default when row not yet created — no INSERT.
                breaker_item = BreakerOpsItem(
                    key=AMOCRM_BUSINESS_WRITES_BREAKER_KEY,
                    state="CLOSED",
                    failure_count=0,
                    opened_at=None,
                    updated_at=now,
                )
            else:
                breaker_item = BreakerOpsItem(
                    key=breaker.key,
                    state=breaker.state.value,
                    failure_count=breaker.failure_count,
                    opened_at=breaker.opened_at,
                    updated_at=breaker.updated_at,
                )
            return TeyaOpsSnapshot(
                pendings=[
                    PendingOpsItem(
                        pending_id=str(row.id),
                        state=row.state,
                        result_code=row.result_code,
                        manual_review_reason=row.manual_review_reason,
                        attempt_count=row.attempt_count,
                        max_attempts=row.max_attempts,
                        next_retry_at=row.next_retry_at,
                        updated_at=row.updated_at,
                        amocrm_contact_id=row.amocrm_contact_id,
                        amocrm_deal_id=row.amocrm_deal_id,
                        amocrm_task_id=row.amocrm_task_id,
                    )
                    for row in rows
                ],
                breaker=breaker_item,
            )

    return router
