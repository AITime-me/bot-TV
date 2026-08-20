"""External structured confirm action for synthetic inbound (SELF-BOOKING-COMMAND-03D/03J).

CONFIRM_SELECTED_SLOT is an explicit action field — never inferred from text/LLM.
Envelope supplies channel / external_message_id / external_conversation_id.
Required ``pii_admission_request_id`` is an opaque request identity only (same
canonical contract as PII admission). Forbidden on this contract: wall-clock
slot time, plaintext PII, PII refs, idempotency key. Schema-only stage: no PII
lookup, no pending admission, and no CREATE.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.booking_create_remote import parse_bot_slot_id
from app.core.self_booking_create_types import require_true_consent
from app.core.self_booking_pii_admission_types import (
    REQUEST_ID_MAX_LENGTH,
    require_pii_admission_request_id,
)

__all__ = (
    "CONFIRM_SELECTED_SLOT_KIND",
    "SyntheticConfirmSelectedSlotAction",
)

CONFIRM_SELECTED_SLOT_KIND: Literal["CONFIRM_SELECTED_SLOT"] = "CONFIRM_SELECTED_SLOT"

_MAX_SLOT_ID_LENGTH = 128


class SyntheticConfirmSelectedSlotAction(BaseModel):
    """Client-facing confirm of a previously offered slot."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["CONFIRM_SELECTED_SLOT"]
    slot_id: str = Field(min_length=1, max_length=_MAX_SLOT_ID_LENGTH)
    pii_admission_request_id: str = Field(
        min_length=1, max_length=REQUEST_ID_MAX_LENGTH
    )
    personal_data_consent: Literal[True]
    offer_acknowledgement: Literal[True]

    @field_validator("slot_id")
    @classmethod
    def _canonical_slot_id(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("slot_id invalid")
        try:
            parse_bot_slot_id(value)
        except ValueError as exc:
            raise ValueError("slot_id invalid") from exc
        return value

    @field_validator("pii_admission_request_id")
    @classmethod
    def _canonical_pii_admission_request_id(cls, value: object) -> str:
        try:
            return require_pii_admission_request_id(value)
        except ValueError as exc:
            raise ValueError("pii_admission_request_id invalid") from exc

    @field_validator("personal_data_consent", "offer_acknowledgement", mode="before")
    @classmethod
    def _exact_true_consent(cls, value: object) -> Literal[True]:
        # Fail closed: reject "true", 1, and other truthy coercions before Literal.
        return require_true_consent(value, field="consent")

    def wire_dict(self) -> dict[str, Any]:
        """JSON for ingress envelope only. No PII, slot time, or idempotency."""

        return {
            "kind": self.kind,
            "slot_id": self.slot_id,
            "pii_admission_request_id": self.pii_admission_request_id,
            "personal_data_consent": True,
            "offer_acknowledgement": True,
        }

    def redacted_view(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "slot_id": "<redacted>",
            "pii_admission_request_id": "<redacted>",
            "personal_data_consent": True,
            "offer_acknowledgement": True,
        }

    def __repr__(self) -> str:
        return f"SyntheticConfirmSelectedSlotAction({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()
