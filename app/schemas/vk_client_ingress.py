"""Typed VK CLIENT durable ingress / inbound envelopes (shadow observer)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.channels.vk_client_types import VK_CLIENT_TEXT_MAX_LEN
from app.core.pii_gateway import safe_fingerprint
from app.models.conversation import Channel
from app.models.ingress import IngressChannel, IngressEventType


class VkClientIngressEvent(BaseModel):
    """Durable VK client ingress envelope.

    extra="forbid" rejects token/signature/PII-shaped fields. Text is storage-only
    for downstream inbox history; never logged, repr'd, or placed in exceptions.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["vk"] = IngressChannel.VK.value
    external_event_id: str = Field(min_length=1, max_length=128)
    external_conversation_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["VK_CLIENT_MESSAGE"] = (
        IngressEventType.VK_CLIENT_MESSAGE.value
    )
    text: str = Field(min_length=1, max_length=VK_CLIENT_TEXT_MAX_LEN, repr=False)
    received_at: datetime | None = None
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
        return Channel.VK

    def correlation_id_or_new(self) -> uuid.UUID:
        return self.correlation_id if self.correlation_id is not None else uuid.uuid4()

    def received_at_utc(self) -> datetime:
        if self.received_at is None:
            return datetime.now(timezone.utc)
        if self.received_at.tzinfo is None:
            return self.received_at.replace(tzinfo=timezone.utc)
        return self.received_at.astimezone(timezone.utc)

    def safe_envelope(self) -> dict[str, Any]:
        """Storage-only envelope with plaintext text for PostgreSQL persistence.

        Never use for logs, repr, diagnostics, or exception messages.
        """

        envelope: dict[str, Any] = {
            "schema": "vk.client.ingress.v1",
            "event_type": self.event_type,
            "text": self.text,
        }
        if self.received_at is not None:
            envelope["received_at"] = self.received_at_utc().isoformat()
        return envelope

    def to_inbound(self) -> VkClientInboundEvent:
        return VkClientInboundEvent(
            channel="vk",
            external_conversation_id=self.external_conversation_id,
            external_message_id=self.external_event_id,
            text=self.text,
            received_at=self.received_at,
        )

    def redacted_view(self) -> dict[str, Any]:
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
        }

    def __repr__(self) -> str:
        return f"VkClientIngressEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()


class VkClientMessageReplyIngressEvent(BaseModel):
    """Durable technical envelope for VK ``message_reply`` (no text/PII)."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["vk"] = IngressChannel.VK.value
    external_event_id: str = Field(min_length=1, max_length=128)
    external_conversation_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["VK_CLIENT_MESSAGE_REPLY"] = (
        IngressEventType.VK_CLIENT_MESSAGE_REPLY.value
    )
    group_id: int = Field(gt=0)
    peer_id: int = Field(gt=0)
    conversation_message_id: int = Field(gt=0)
    provider_message_id: int = Field(gt=0)
    occurred_at: datetime
    random_id: int | None = Field(default=None, ge=0)
    # Bounded technical provenance only (dict or JSON string).
    payload: dict[str, Any] | str | None = None
    correlation_id: uuid.UUID | None = None

    @field_validator("external_event_id", "external_conversation_id")
    @classmethod
    def _safe_external_id(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("external id must be alphanumeric with -/_")
        return value

    def correlation_id_or_new(self) -> uuid.UUID:
        return self.correlation_id if self.correlation_id is not None else uuid.uuid4()

    def safe_envelope(self) -> dict[str, Any]:
        envelope: dict[str, Any] = {
            "schema": "vk.client.message_reply.v1",
            "event_type": self.event_type,
            "group_id": self.group_id,
            "peer_id": self.peer_id,
            "conversation_message_id": self.conversation_message_id,
            "provider_message_id": self.provider_message_id,
            "occurred_at": self.occurred_at.astimezone(timezone.utc).isoformat()
            if self.occurred_at.tzinfo is not None
            else self.occurred_at.replace(tzinfo=timezone.utc).isoformat(),
            "external_conversation_id": self.external_conversation_id,
        }
        if self.random_id is not None:
            envelope["random_id"] = self.random_id
        if self.payload is not None:
            if type(self.payload) is dict and len(self.payload) <= 8:
                envelope["payload"] = self.payload
            elif type(self.payload) is str and 0 < len(self.payload) <= 1000:
                envelope["payload"] = self.payload
        return envelope

    def redacted_view(self) -> dict[str, Any]:
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
            "group_id": self.group_id,
            "payload": "<redacted>" if self.payload is not None else None,
        }

    def __repr__(self) -> str:
        return f"VkClientMessageReplyIngressEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()


class VkClientInboundEvent(BaseModel):
    """Normalized VK client inbound for observer persistence."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["vk"] = "vk"
    external_conversation_id: str = Field(min_length=1, max_length=128)
    external_message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=VK_CLIENT_TEXT_MAX_LEN, repr=False)
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
        return Channel.VK

    def received_at_utc(self) -> datetime:
        if self.received_at is None:
            return datetime.now(timezone.utc)
        if self.received_at.tzinfo is None:
            return self.received_at.replace(tzinfo=timezone.utc)
        return self.received_at.astimezone(timezone.utc)

    def safe_payload(self) -> dict[str, Any]:
        """Storage-only payload; DialogContext reads payload_json->>'text'."""

        return {
            "schema": "vk.client.inbound.v1",
            "text": self.text,
        }

    def redacted_view(self) -> dict[str, Any]:
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
        return f"VkClientInboundEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()
