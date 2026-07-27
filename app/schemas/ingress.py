from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.conversation import Channel
from app.models.ingress import IngressEventType


class SyntheticIngressEvent(BaseModel):
    """Synthetic-only durable ingress envelope.

    extra="forbid" rejects PII-shaped fields (phone, email, token, signature).
    Text is kept only for downstream foundation persistence; it must never be
    logged, repr'd, or placed in exception messages.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["synthetic"] = "synthetic"
    external_event_id: str = Field(min_length=1, max_length=128)
    external_conversation_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["SYNTHETIC_MESSAGE"] = IngressEventType.SYNTHETIC_MESSAGE.value
    text: str = Field(min_length=1, max_length=2000)
    correlation_id: uuid.UUID | None = None

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
        """Minimal envelope stored in PostgreSQL for later worker processing.

        Justified fields: schema marker, normalized type, and synthetic fixture
        text required to call InboundService. No tokens, signatures, phones,
        emails, or raw provider headers.
        """
        return {
            "schema": "synthetic.ingress.v1",
            "event_type": self.event_type,
            "text": self.text,
        }

    def redacted_view(self) -> dict[str, Any]:
        """Safe projection for logs, repr, and assertion diagnostics."""
        return {
            "channel": self.channel,
            "external_event_id": self.external_event_id,
            "external_conversation_id": self.external_conversation_id,
            "event_type": self.event_type,
            "correlation_id": (
                str(self.correlation_id) if self.correlation_id is not None else None
            ),
            "text": "<redacted>",
        }

    def __repr__(self) -> str:
        return f"SyntheticIngressEvent({self.redacted_view()!r})"
