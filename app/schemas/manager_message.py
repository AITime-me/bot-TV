from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.pii_gateway import safe_fingerprint
from app.models.conversation import Channel


class SyntheticManagerMessageEvent(BaseModel):
    """Normalized synthetic manager message.

    ``provider_sequence`` is the only ordering contract. A missing sequence is
    accepted at the schema boundary solely so the service can persist a
    QUARANTINED audit row; it can never affect the dialog FSM.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["synthetic"] = "synthetic"
    external_conversation_id: str = Field(min_length=1, max_length=128)
    external_message_id: str = Field(min_length=1, max_length=128)
    provider_sequence: int | None = Field(
        default=None,
        ge=0,
        le=9_223_372_036_854_775_807,
    )
    provider_occurred_at: datetime | None = None
    text: str = Field(min_length=1, max_length=4000, repr=False)

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

    def provider_occurred_at_utc(self) -> datetime | None:
        moment = self.provider_occurred_at
        if moment is None:
            return None
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def redacted_view(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "external_conversation_id": safe_fingerprint(
                self.external_conversation_id,
                purpose="external_conversation_id",
            ),
            "external_message_id": safe_fingerprint(
                self.external_message_id,
                purpose="external_message_id",
            ),
            "provider_sequence": self.provider_sequence,
            "provider_occurred_at": self.provider_occurred_at_utc(),
            "text": "<redacted>",
        }

    def __repr__(self) -> str:
        return f"SyntheticManagerMessageEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()
