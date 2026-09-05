"""Typed VK CLIENT durable ingress / inbound envelopes (shadow observer)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.channels.vk_client_types import VK_CLIENT_TEXT_MAX_LEN
from app.channels.vk_client_outbound_provenance import (
    VkReplyPayloadKind,
    VkReplyProvenanceTechnical,
)
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
    # Technical provenance only — never raw foreign payload / free text.
    provenance_kind: Literal["ABSENT", "FOREIGN", "BOT_TV_CANDIDATE"]
    provenance_v: int | None = None
    provenance_ns: str | None = None
    provenance_oid: str | None = None
    provenance_mac: str | None = None
    correlation_id: uuid.UUID | None = None

    @field_validator("external_event_id", "external_conversation_id")
    @classmethod
    def _safe_external_id(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("external id must be alphanumeric with -/_")
        return value

    @model_validator(mode="after")
    def _provenance_technical_only(self) -> VkClientMessageReplyIngressEvent:
        if self.provenance_kind != "BOT_TV_CANDIDATE":
            if (
                self.provenance_v is not None
                or self.provenance_ns is not None
                or self.provenance_oid is not None
                or self.provenance_mac is not None
            ):
                raise ValueError("INVALID_PROVENANCE_TECHNICAL")
        # Validates allowlisted candidate shape/types/lengths.
        self.provenance_technical()
        return self

    def correlation_id_or_new(self) -> uuid.UUID:
        return self.correlation_id if self.correlation_id is not None else uuid.uuid4()

    def provenance_technical(self) -> VkReplyProvenanceTechnical:
        kind = VkReplyPayloadKind(self.provenance_kind)
        if kind is VkReplyPayloadKind.BOT_TV_CANDIDATE:
            return VkReplyProvenanceTechnical(
                kind=kind,
                v=self.provenance_v,
                ns=self.provenance_ns,
                oid=self.provenance_oid,
                mac=self.provenance_mac,
            )
        return VkReplyProvenanceTechnical(kind=kind)

    def safe_envelope(self) -> dict[str, Any]:
        provenance = self.provenance_technical()
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
            "provenance": provenance.to_envelope_fragment(),
        }
        if self.random_id is not None:
            envelope["random_id"] = self.random_id
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
            "provenance_kind": self.provenance_kind,
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
