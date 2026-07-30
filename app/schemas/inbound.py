from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pii_gateway import safe_fingerprint
from app.models.conversation import Channel


class SyntheticInboundEvent(BaseModel):
    """Normalized synthetic inbound event for foundation tests/services.

    Only synthetic fixture data is accepted. extra="forbid" rejects accidental
    PII-shaped fields (phone, email, tokens, etc.) at the schema boundary.
    This stage does not claim full PII filtering of free-form text.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["synthetic"] = "synthetic"
    external_conversation_id: str = Field(min_length=1, max_length=128)
    external_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000, repr=False)
    received_at: datetime | None = None

    @field_validator("external_conversation_id", "external_message_id")
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

    def received_at_utc(self) -> datetime:
        if self.received_at is None:
            return datetime.now(timezone.utc)
        if self.received_at.tzinfo is None:
            return self.received_at.replace(tzinfo=timezone.utc)
        return self.received_at.astimezone(timezone.utc)

    def safe_payload(self) -> dict[str, Any]:
        """Storage-only payload with plaintext text for PostgreSQL persistence.

        Never use for logs, repr, diagnostics, or exception messages.
        """
        return {
            "schema": "synthetic.inbound.v1",
            "text": self.text,
        }

    def redacted_view(self) -> dict[str, Any]:
        """Safe projection for logs, repr, and assertion diagnostics."""
        return {
            "channel": self.channel,
            "external_message_id": safe_fingerprint(
                self.external_message_id,
                purpose="external_message_id",
            ),
            "external_conversation_id": safe_fingerprint(
                self.external_conversation_id,
                purpose="external_conversation_id",
            ),
            "text": "<redacted>",
        }

    def __repr__(self) -> str:
        return f"SyntheticInboundEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()
