"""HTTP schemas for closed-test surface (BOT-CLOSED-TEST-01A)."""

from __future__ import annotations

import re
import uuid
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SAFE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


class ClosedTestEventCreate(BaseModel):
    """Narrow synthetic-only intake. Channel is always server-side synthetic."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000, repr=False)

    @field_validator("session_id", "request_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if _SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError("id must be ASCII alphanumeric with -/_")
        return value

    @field_validator("text")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("text contains control characters")
        return value

    def __repr__(self) -> str:
        return (
            "ClosedTestEventCreate("
            f"session_id={self.session_id!r}, "
            f"request_id={self.request_id!r}, "
            "text=<redacted>)"
        )


class ClosedTestEventAck(BaseModel):
    """Safe POST acknowledgement — never echoes inbound text."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    duplicate: bool
    event_id: uuid.UUID
    status: str
    correlation_id: uuid.UUID


class ClosedTestStageIngress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    channel: Literal["synthetic"] = "synthetic"


class ClosedTestStageInbound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool = True
    processing_status: str


class ClosedTestStageReplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool = True
    reply_plan_id: uuid.UUID
    status: str
    context_version: int


class ClosedTestStageOutbound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool = True
    destination_type: Literal["SYNTHETIC_OUTBOUND"] = "SYNTHETIC_OUTBOUND"
    delivery_status: str
    outbound_id: uuid.UUID


class ClosedTestEventStatus(BaseModel):
    """Read-only pipeline projection for admin polling."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID
    correlation_id: uuid.UUID
    ingress: ClosedTestStageIngress
    inbound: ClosedTestStageInbound | None = None
    reply_plan: ClosedTestStageReplyPlan | None = None
    outbound: ClosedTestStageOutbound | None = None
    synthetic_result: dict[str, Any] | None = None


class ClosedTestPiiAdmissionCreate(BaseModel):
    """Pre-durability PII admission intake. Never persisted to ingress/Inbox."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    client_name: str = Field(min_length=1, max_length=256, repr=False)
    phone: str = Field(min_length=1, max_length=32, repr=False)

    @field_validator("session_id", "request_id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if _SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError("id must be ASCII alphanumeric with -/_")
        return value

    @field_validator("client_name", "phone")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("value contains control characters")
        return value

    def __repr__(self) -> str:
        return (
            "ClosedTestPiiAdmissionCreate("
            f"session_id={self.session_id!r}, "
            f"request_id={self.request_id!r}, "
            "client_name=<redacted>, "
            "phone=<redacted>)"
        )


class ClosedTestPiiAdmissionAck(BaseModel):
    """Safe PII admission acknowledgement — never returns opaque refs or PII."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    reused: bool
    session_id: str
    request_id: str
    status: Literal["ADMITTED", "REUSED"]

    def __repr__(self) -> str:
        return (
            "ClosedTestPiiAdmissionAck("
            f"accepted={self.accepted!r}, "
            f"reused={self.reused!r}, "
            f"session_id={self.session_id!r}, "
            f"request_id={self.request_id!r}, "
            f"status={self.status!r})"
        )
