"""Strict optional synthetic booking fixtures (CURSOR-20/23).

Confirmed service/master IDs, slots/availability query, flags, and decision
time only. No free-text intent parsing. No PII, tokens, or URLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.core.booking_availability_http import (
    require_calendar_date,
    require_calendar_month,
)

_MAX_ID_LENGTH = 128
_MAX_SLOTS = 32


def _require_safe_id(value: str) -> str:
    if not value or len(value) > _MAX_ID_LENGTH:
        raise ValueError("id invalid")
    if any(ch.isspace() for ch in value):
        raise ValueError("id invalid")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("id invalid")
    return value


class SyntheticBookingSlot(BaseModel):
    """Backend-provided slot fixture. Times are never invented by the bot."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    starts_at: datetime
    master_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    service_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)

    @field_validator("slot_id", "master_id", "service_id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _require_safe_id(value)

    @field_validator("starts_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("starts_at must be timezone-aware")
        return value

    def wire_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "starts_at": self.starts_at.isoformat(),
            "master_id": self.master_id,
            "service_id": self.service_id,
        }


class SyntheticAvailableDaysQuery(BaseModel):
    """Typed read-only request for available calendar days."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["AVAILABLE_DAYS"]
    month: str

    @field_validator("month")
    @classmethod
    def _month(cls, value: str) -> str:
        try:
            return require_calendar_month(value)
        except Exception as exc:
            raise ValueError("month invalid") from exc

    def wire_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "month": self.month}


class SyntheticAvailableSlotsQuery(BaseModel):
    """Typed read-only request for slots on one calendar day."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["SLOTS"]
    date: str

    @field_validator("date")
    @classmethod
    def _date(cls, value: str) -> str:
        try:
            return require_calendar_date(value)
        except Exception as exc:
            raise ValueError("date invalid") from exc

    def wire_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "date": self.date}


SyntheticAvailabilityQuery = Annotated[
    Union[SyntheticAvailableDaysQuery, SyntheticAvailableSlotsQuery],
    Field(discriminator="kind"),
]


class SyntheticBookingInput(BaseModel):
    """Optional booking fixture for synthetic ingress/inbound only."""

    model_config = ConfigDict(extra="forbid")

    service_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    master_id: str | None = None
    include_alternatives: bool
    alternate_master_consent: bool = False
    slots: tuple[SyntheticBookingSlot, ...] = ()
    availability_query: SyntheticAvailabilityQuery | None = None
    decision_at: datetime

    @field_validator("service_id")
    @classmethod
    def _service_id(cls, value: str) -> str:
        return _require_safe_id(value)

    @field_validator("master_id")
    @classmethod
    def _master_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_safe_id(value)

    @field_validator("slots")
    @classmethod
    def _bounded_slots(
        cls, value: tuple[SyntheticBookingSlot, ...]
    ) -> tuple[SyntheticBookingSlot, ...]:
        if len(value) > _MAX_SLOTS:
            raise ValueError("too many slots")
        return value

    @field_validator("decision_at")
    @classmethod
    def _decision_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _slots_xor_availability_query(self) -> SyntheticBookingInput:
        if self.availability_query is not None and self.slots:
            raise ValueError("slots and availability_query are mutually exclusive")
        return self

    def wire_dict(self) -> dict[str, Any]:
        """Safe JSON for reply-plan payload. No text, tokens, URLs, or PII."""

        payload: dict[str, Any] = {
            "service_id": self.service_id,
            "include_alternatives": self.include_alternatives,
            "alternate_master_consent": self.alternate_master_consent,
            "decision_at": self.decision_at.isoformat(),
            "slots": [slot.wire_dict() for slot in self.slots],
        }
        if self.master_id is not None:
            payload["master_id"] = self.master_id
        if self.availability_query is not None:
            payload["availability_query"] = self.availability_query.wire_dict()
        return payload
