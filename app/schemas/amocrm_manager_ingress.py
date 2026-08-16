"""Schemas for amoCRM manager durable ingress (AMO-01A / AMO-01B1)."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.amocrm_manager_ids import (
    AMOCRM_MANAGER_EXTERNAL_ID_MAX_LENGTH,
    amocrm_manager_namespaced_id,
)
from app.core.pii_gateway import safe_fingerprint
from app.models.ingress import IngressChannel, IngressEventType


def _safe_amo_component_id(value: str) -> str:
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ValueError("external id must be alphanumeric with -/_")
    if ":" in value:
        raise ValueError("external id must not contain ':'")
    return value


def _lift_conversation_client_id(data: Any) -> Any:
    """Extract message.conversation.client_id without weakening extra=forbid."""

    if not isinstance(data, dict):
        return data
    raw = dict(data)
    extracted = raw.get("conversation_client_id")
    message = raw.pop("message", None)
    if isinstance(message, dict):
        conversation = message.get("conversation")
        if isinstance(conversation, dict):
            client_id = conversation.get("client_id")
            if type(client_id) is str and client_id.strip():
                extracted = client_id.strip()
    conversation = raw.pop("conversation", None)
    if isinstance(conversation, dict):
        client_id = conversation.get("client_id")
        if type(client_id) is str and client_id.strip():
            extracted = extracted or client_id.strip()
    if type(extracted) is str and extracted.strip():
        raw["conversation_client_id"] = extracted.strip()
    elif "conversation_client_id" in raw and raw["conversation_client_id"] is None:
        raw.pop("conversation_client_id", None)
    return raw


def _normalize_official_v2_chat_webhook(data: dict[str, Any]) -> dict[str, Any]:
    """Map official Chat API v2 webhook body → flat AmoCrmChatWebhookPayload.

    Strips outer envelope fields (account_id, receiver PII, media, …) so
    ``extra=forbid`` stays strict. Non-text message types fail closed.
    Never logs the body.
    """

    message = data.get("message")
    if not isinstance(message, dict):
        raise ValueError("unsupported webhook shape")
    conversation = message.get("conversation")
    inner = message.get("message")
    if not isinstance(conversation, dict) or not isinstance(inner, dict):
        raise ValueError("unsupported webhook shape")

    msg_type = inner.get("type")
    if msg_type != "text":
        raise ValueError("unsupported message type")

    chat_id = conversation.get("id")
    message_id = inner.get("id")
    text = inner.get("text")
    msec_timestamp = message.get("msec_timestamp")
    if type(chat_id) is not str or type(message_id) is not str or type(text) is not str:
        raise ValueError("unsupported webhook shape")
    if type(msec_timestamp) is not int:
        raise ValueError("unsupported webhook shape")

    normalized: dict[str, Any] = {
        "amocrm_chat_id": chat_id,
        "message_id": message_id,
        "provider_sequence": msec_timestamp,
        "text": text,
    }
    client_id = conversation.get("client_id")
    if type(client_id) is str and client_id.strip():
        normalized["conversation_client_id"] = client_id.strip()
    return normalized


def _normalize_amocrm_chat_webhook_payload(data: Any) -> Any:
    """Accept official v2 nested body or existing flat test/internal payload."""

    if not isinstance(data, dict):
        return data
    raw = dict(data)
    message = raw.get("message")
    # Official v2: message.message is the nested Chat message object.
    if isinstance(message, dict) and isinstance(message.get("message"), dict):
        return _normalize_official_v2_chat_webhook(raw)
    return _lift_conversation_client_id(raw)


class AmoCrmManagerIngressEvent(BaseModel):
    """Normalized amoCRM manager webhook envelope for durable ingress.

    extra="forbid" rejects PII-shaped fields. Text is storage-only and must
    never be logged, repr'd, or placed in exception/audit meta beyond the
    existing manager_messages body_text path.

    ``external_message_id`` is the namespaced manager/ingress key
    (``amo:{chat}:{message}``). Raw amo message id is kept as
    ``amocrm_message_id`` for provenance without a manager_messages channel
    migration.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Literal["amocrm"] = IngressChannel.AMOCRM.value
    event_type: Literal["AMOCRM_MANAGER_MESSAGE"] = (
        IngressEventType.AMOCRM_MANAGER_MESSAGE.value
    )
    amocrm_chat_id: str = Field(min_length=1, max_length=128)
    amocrm_message_id: str = Field(min_length=1, max_length=128)
    external_message_id: str = Field(
        min_length=1,
        max_length=AMOCRM_MANAGER_EXTERNAL_ID_MAX_LENGTH,
    )
    provider_sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    text: str = Field(min_length=1, max_length=4000, repr=False)
    conversation_client_id: str | None = Field(default=None, max_length=128)
    correlation_id: uuid.UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_conversation_client_id(cls, data: Any) -> Any:
        return _lift_conversation_client_id(data)

    @field_validator("amocrm_chat_id", "amocrm_message_id")
    @classmethod
    def _safe_component_id(cls, value: str) -> str:
        return _safe_amo_component_id(value)

    @field_validator("conversation_client_id")
    @classmethod
    def _safe_conversation_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_amo_component_id(value)

    @field_validator("external_message_id")
    @classmethod
    def _safe_namespaced_id(cls, value: str) -> str:
        # Namespaced form allows ':' separators; components stay alphanumeric.
        stripped = value.replace("-", "").replace("_", "").replace(":", "")
        if not stripped.isalnum():
            raise ValueError("external id must be alphanumeric with -/_/:")
        return value

    @field_validator("text")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("text contains control characters")
        return value

    @model_validator(mode="after")
    def _namespaced_id_matches_components(self) -> AmoCrmManagerIngressEvent:
        expected = amocrm_manager_namespaced_id(
            amocrm_chat_id=self.amocrm_chat_id,
            amocrm_message_id=self.amocrm_message_id,
        )
        if self.external_message_id != expected:
            raise ValueError("external_message_id must match amo chat/message namespace")
        return self

    def correlation_id_or_new(self) -> uuid.UUID:
        return self.correlation_id if self.correlation_id is not None else uuid.uuid4()

    def safe_envelope(self) -> dict[str, Any]:
        """Storage-only envelope. Never use for logs/repr/diagnostics."""

        envelope: dict[str, Any] = {
            "schema": "amocrm.manager.ingress.v1",
            "event_type": self.event_type,
            "amocrm_chat_id": self.amocrm_chat_id,
            "amocrm_message_id": self.amocrm_message_id,
            "external_message_id": self.external_message_id,
            "provider_sequence": self.provider_sequence,
            "text": self.text,
        }
        if self.conversation_client_id is not None:
            envelope["conversation_client_id"] = self.conversation_client_id
        return envelope

    def redacted_view(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "event_type": self.event_type,
            "amocrm_chat_id": safe_fingerprint(
                self.amocrm_chat_id,
                purpose="amocrm_chat_id",
            ),
            "amocrm_message_id": safe_fingerprint(
                self.amocrm_message_id,
                purpose="amocrm_message_id",
            ),
            "external_message_id": safe_fingerprint(
                self.external_message_id,
                purpose="external_message_id",
            ),
            "provider_sequence": self.provider_sequence,
            "conversation_client_id": (
                safe_fingerprint(
                    self.conversation_client_id,
                    purpose="conversation_client_id",
                )
                if self.conversation_client_id is not None
                else None
            ),
            "correlation_id": (
                safe_fingerprint(str(self.correlation_id), purpose="correlation_id")
                if self.correlation_id is not None
                else None
            ),
            "text": "<redacted>",
        }

    def __repr__(self) -> str:
        return f"AmoCrmManagerIngressEvent({self.redacted_view()!r})"

    def __str__(self) -> str:
        return self.__repr__()


class AmoCrmChatWebhookPayload(BaseModel):
    """HTTP body accepted by the amoCRM Chat manager webhook (01A/01B1).

    Accepts the official Chat API v2 nested webhook shape (normalized in
    ``before``) or the existing flat internal/test payload. HMAC is verified
    on the raw body before this model runs.
    """

    model_config = ConfigDict(extra="forbid")

    amocrm_chat_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    provider_sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    text: str = Field(min_length=1, max_length=4000, repr=False)
    conversation_client_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def _normalize_webhook_shape(cls, data: Any) -> Any:
        return _normalize_amocrm_chat_webhook_payload(data)

    @field_validator("amocrm_chat_id", "message_id")
    @classmethod
    def _safe_external_id(cls, value: str) -> str:
        return _safe_amo_component_id(value)

    @field_validator("conversation_client_id")
    @classmethod
    def _safe_conversation_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_amo_component_id(value)

    @field_validator("text")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
            raise ValueError("text contains control characters")
        return value

    def to_ingress_event(self) -> AmoCrmManagerIngressEvent:
        namespaced = amocrm_manager_namespaced_id(
            amocrm_chat_id=self.amocrm_chat_id,
            amocrm_message_id=self.message_id,
        )
        return AmoCrmManagerIngressEvent(
            amocrm_chat_id=self.amocrm_chat_id,
            amocrm_message_id=self.message_id,
            external_message_id=namespaced,
            provider_sequence=self.provider_sequence,
            text=self.text,
            conversation_client_id=self.conversation_client_id,
        )
