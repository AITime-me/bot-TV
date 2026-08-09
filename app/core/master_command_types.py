"""Master command flow types (CURSOR-28).

Channel-agnostic envelope + durable confirmation state machine.
No VK/MAX SDK. No live webhook wiring. master_id never appears in user results.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from app.core.master_channel_binding import (
    DEFAULT_CONNECTION_SCOPE,
    MasterBindingChannel,
    normalize_connection_scope,
    normalize_external_account_id,
    require_master_binding_channel,
)
from app.core.manager_working_hours import MANAGER_TIMEZONE, to_manager_local

__all__ = (
    "CONFIRMATION_TTL_SECONDS",
    "EXECUTION_LEASE_SECONDS",
    "INBOUND_MESSAGE_ID_MAX_LENGTH",
    "MasterCommandEnvelope",
    "MasterCommandKind",
    "MasterCommandPendingState",
    "MasterCommandFlowOutcome",
    "MasterCommandFlowResult",
    "MasterCommandClarificationNeed",
    "MasterCommandPreview",
    "MasterCommandSafePayload",
    "ACTIVE_PENDING_STATES",
    "TERMINAL_PENDING_STATES",
    "CONFIRM_TEXT_TOKENS",
    "CANCEL_TEXT_TOKENS",
    "master_command_pii_conversation_id",
    "normalize_inbound_message_id",
    "normalize_master_command_text",
    "build_master_command_envelope",
)

CONFIRMATION_TTL_SECONDS: Final[int] = 15 * 60
EXECUTION_LEASE_SECONDS: Final[int] = 60
INBOUND_MESSAGE_ID_MAX_LENGTH: Final[int] = 128

_INBOUND_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]+$")
_PII_NS: Final[uuid.UUID] = uuid.UUID("6b2c8f1a-4e9d-4a71-9c3b-28f0d1a7e5c4")


class MasterCommandKind(str, enum.Enum):
    CLOSE_INTERVAL = "CLOSE_INTERVAL"
    CLOSE_DAY = "CLOSE_DAY"
    CREATE_BOOKING = "CREATE_BOOKING"
    SCHEDULE_READ = "SCHEDULE_READ"


class MasterCommandPendingState(str, enum.Enum):
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


ACTIVE_PENDING_STATES: Final[frozenset[MasterCommandPendingState]] = frozenset(
    {
        MasterCommandPendingState.AWAITING_CLARIFICATION,
        MasterCommandPendingState.AWAITING_CONFIRMATION,
        MasterCommandPendingState.EXECUTING,
    }
)

TERMINAL_PENDING_STATES: Final[frozenset[MasterCommandPendingState]] = frozenset(
    {
        MasterCommandPendingState.SUCCEEDED,
        MasterCommandPendingState.FAILED,
        MasterCommandPendingState.CANCELLED,
        MasterCommandPendingState.EXPIRED,
    }
)


class MasterCommandClarificationNeed(str, enum.Enum):
    DATE = "DATE"
    TIME = "TIME"
    END_TIME = "END_TIME"
    SLOT_ID = "SLOT_ID"
    CLIENT_NAME = "CLIENT_NAME"
    PHONE = "PHONE"
    BLOCK_TYPE = "BLOCK_TYPE"
    AMBIGUOUS = "AMBIGUOUS"


class MasterCommandFlowOutcome(str, enum.Enum):
    COMMAND_ACCEPTED = "COMMAND_ACCEPTED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CANCELLED = "CANCELLED"
    SUCCESS = "SUCCESS"
    CONFLICT = "CONFLICT"
    UNAVAILABLE = "UNAVAILABLE"
    MANUAL_HELP = "MANUAL_HELP"
    BINDING_REQUIRED = "BINDING_REQUIRED"
    BINDING_AMBIGUOUS = "BINDING_AMBIGUOUS"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    REJECTED = "REJECTED"


CONFIRM_TEXT_TOKENS: Final[frozenset[str]] = frozenset(
    {"да", "подтверждаю", "подтвердить", "ок", "ok", "+"}
)
CANCEL_TEXT_TOKENS: Final[frozenset[str]] = frozenset(
    {"нет", "отмена", "отменить", "cancel", "-"}
)


@dataclass(frozen=True, slots=True, repr=False)
class MasterCommandSafePayload:
    """Non-PII command arguments persisted in pending rows."""

    date_key: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    block_type: str | None = None
    slot_id: str | None = None
    from_date_key: str | None = None
    to_date_key: str | None = None
    missing: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.date_key is not None:
            out["dateKey"] = self.date_key
        if self.start_time is not None:
            out["startTime"] = self.start_time
        if self.end_time is not None:
            out["endTime"] = self.end_time
        if self.block_type is not None:
            out["blockType"] = self.block_type
        if self.slot_id is not None:
            out["slotId"] = self.slot_id
        if self.from_date_key is not None:
            out["fromDateKey"] = self.from_date_key
        if self.to_date_key is not None:
            out["toDateKey"] = self.to_date_key
        if self.missing:
            out["missing"] = list(self.missing)
        return out

    @classmethod
    def from_json_dict(cls, value: object) -> MasterCommandSafePayload:
        if type(value) is not dict:
            return cls()
        missing_raw = value.get("missing")
        missing: tuple[str, ...] = ()
        if type(missing_raw) is list:
            missing = tuple(
                item for item in missing_raw if type(item) is str and item
            )
        return cls(
            date_key=_opt_str(value.get("dateKey")),
            start_time=_opt_str(value.get("startTime")),
            end_time=_opt_str(value.get("endTime")),
            block_type=_opt_str(value.get("blockType")),
            slot_id=_opt_str(value.get("slotId")),
            from_date_key=_opt_str(value.get("fromDateKey")),
            to_date_key=_opt_str(value.get("toDateKey")),
            missing=missing,
        )

    def __repr__(self) -> str:
        return (
            "MasterCommandSafePayload("
            f"date_key={self.date_key!r}, "
            f"start_time={self.start_time!r}, "
            f"end_time={self.end_time!r}, "
            f"block_type={self.block_type!r}, "
            f"slot_id={'<set>' if self.slot_id else None}, "
            f"from_date_key={self.from_date_key!r}, "
            f"to_date_key={self.to_date_key!r}, "
            f"missing={self.missing!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MasterCommandPreview:
    """Safe confirmation preview for the master. No IDs, phones, or names."""

    action: str
    date_key: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    service_hint: str | None = None
    command_version: int | None = None

    def __repr__(self) -> str:
        return (
            "MasterCommandPreview("
            f"action={self.action!r}, "
            f"date_key={self.date_key!r}, "
            f"start_time={self.start_time!r}, "
            f"end_time={self.end_time!r}, "
            f"service_hint={self.service_hint!r}, "
            f"command_version={self.command_version!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MasterCommandEnvelope:
    """Normalized channel-agnostic inbound message."""

    channel: MasterBindingChannel
    connection_scope: str
    external_account_id: str
    external_message_id: str
    text: str
    occurred_at: datetime
    correlation_id: str

    def __repr__(self) -> str:
        return (
            "MasterCommandEnvelope("
            f"channel={self.channel.value!r}, "
            "connection_scope=<redacted>, "
            "external_account_id=<redacted>, "
            "external_message_id=<redacted>, "
            "text=<redacted>, "
            f"occurred_at={self.occurred_at.isoformat()!r}, "
            "correlation_id=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class MasterCommandFlowResult:
    outcome: MasterCommandFlowOutcome
    clarification_needs: tuple[MasterCommandClarificationNeed, ...] = ()
    preview: MasterCommandPreview | None = None
    schedule_summary: tuple[str, ...] = ()
    result_code: str | None = None
    command_kind: MasterCommandKind | None = None
    command_version: int | None = None
    details: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return (
            "MasterCommandFlowResult("
            f"outcome={self.outcome.value!r}, "
            f"clarification_needs={tuple(n.value for n in self.clarification_needs)!r}, "
            f"preview={self.preview!r}, "
            f"schedule_summary_len={len(self.schedule_summary)}, "
            f"result_code={self.result_code!r}, "
            f"command_kind="
            f"{None if self.command_kind is None else self.command_kind.value!r}, "
            f"command_version={self.command_version!r})"
        )


def _opt_str(value: object) -> str | None:
    if type(value) is not str or not value:
        return None
    return value


def normalize_inbound_message_id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("INVALID_INPUT") from None
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > INBOUND_MESSAGE_ID_MAX_LENGTH
        or _INBOUND_ID_RE.fullmatch(cleaned) is None
    ):
        raise ValueError("INVALID_INPUT") from None
    return cleaned


def normalize_master_command_text(value: object) -> str:
    if type(value) is not str:
        raise ValueError("INVALID_INPUT") from None
    # Collapse whitespace; keep original case for name extraction separately.
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 2000:
        raise ValueError("INVALID_INPUT") from None
    return collapsed


def master_command_pii_conversation_id(
    *,
    channel: MasterBindingChannel | str,
    connection_scope: str,
    external_account_id: str,
) -> uuid.UUID:
    """Deterministic UUID5 binding for ephemeral PII (no Conversation FK)."""

    ch = channel.value if isinstance(channel, MasterBindingChannel) else str(channel)
    name = f"{ch}|{connection_scope}|{external_account_id}"
    return uuid.uuid5(_PII_NS, name)


def build_master_command_envelope(
    *,
    channel: object,
    external_account_id: object,
    external_message_id: object,
    text: object,
    occurred_at: object,
    correlation_id: object | None = None,
    connection_scope: object = DEFAULT_CONNECTION_SCOPE,
) -> MasterCommandEnvelope:
    ch = require_master_binding_channel(channel)
    scope = normalize_connection_scope(connection_scope)
    ext = normalize_external_account_id(external_account_id)
    msg_id = normalize_inbound_message_id(external_message_id)
    normalized_text = normalize_master_command_text(text)
    if type(occurred_at) is not datetime:
        raise ValueError("INVALID_INPUT") from None
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("INVALID_INPUT") from None
    local = to_manager_local(occurred_at)
    if correlation_id is None:
        corr = str(uuid.uuid4())
    elif type(correlation_id) is str and correlation_id.strip():
        corr = correlation_id.strip()[:128]
    else:
        raise ValueError("INVALID_INPUT") from None
    return MasterCommandEnvelope(
        channel=ch,
        connection_scope=scope,
        external_account_id=ext,
        external_message_id=msg_id,
        text=normalized_text,
        occurred_at=local.astimezone(MANAGER_TIMEZONE),
        correlation_id=corr,
    )
