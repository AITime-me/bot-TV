from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pii_gateway import safe_fingerprint
from app.models.conversation import Channel
from app.models.ingress import IngressEventType
from app.schemas.booking_input import SyntheticBookingInput


class SyntheticIngressEvent(BaseModel):
    """Synthetic-only durable ingress envelope.

    extra="forbid" rejects PII-shaped fields (phone, email, token, signature).
    Text is kept only for downstream foundation persistence; it must never be
    logged, repr'd, or placed in exception messages. Optional ``booking`` is a
    typed fixture only — never derived from free-form text.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["synthetic"] = "synthetic"
    external_event_id: str = Field(min_length=1, max_length=128)
    external_conversation_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["SYNTHETIC_MESSAGE"] = IngressEventType.SYNTHETIC_MESSAGE.value
    text: str = Field(min_length=1, max_length=2000, repr=False)
    correlation_id: uuid.UUID | None = None
    booking: SyntheticBookingInput | None = None

    @field_validator("external_event_id", "external_conversation_id")
    @classmethod
    def _safe_external_id(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("external id must be alphanumeric with -/_")
        return value

    @field_validator("text")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("text contains control characters")
        return value

    def channel_enum(self) -> Channel:
        return Channel.SYNTHETIC

    def correlation_id_or_new(self) -> uuid.UUID:
        return self.correlation_id if self.correlation_id is not None else uuid.uuid4()

    def safe_envelope(self) -> dict[str, Any]:
        """Storage-only envelope with plaintext text for PostgreSQL persistence.

        Never use for logs, repr, diagnostics, or exception messages.
        """
        envelope: dict[str, Any] = {
            "schema": "synthetic.ingress.v1",
            "event_type": self.event_type,
            "text": self.text,
        }
        if self.booking is not None:
            envelope["booking"] = self.booking.wire_dict()
        return envelope

    def redacted_view(self) -> dict[str, Any]:
        """Safe projection for logs, repr, and assertion diagnostics."""
        return {
            "channel": self.channel,
            "external_event_id": safe_fingerprint(
                self.external_event_id,
                purpose="external_event_id",
            ),
            "external_conversation_id": safe_fingerprint(
                self.external_conversation_id,
                purpose="external_conversation_id",
            ),
            "event_type": self.event_type,
            "correlation_id": (
                safe_fingerprint(str(self.correlation_id), purpose="correlation_id")
                if self.correlation_id is not None
                else None
            ),
            "text": "<redacted>",
            "booking_present": self.booking is not None,
        }

    def __repr__(self) -> str:
        return f"SyntheticIngressEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()
