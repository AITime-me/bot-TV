"""Master channel binding types and validators (CURSOR-27).

Durable channel-agnostic map: (channel, connection_scope, external_account_id)
→ online-zapis-tv ``masterId``. No name/phone/text matching. No VK/MAX API.
No live channel wiring.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

__all__ = (
    "DEFAULT_CONNECTION_SCOPE",
    "EXTERNAL_ACCOUNT_ID_MAX_LENGTH",
    "CONNECTION_SCOPE_MAX_LENGTH",
    "MasterBindingChannel",
    "MasterBindingStatus",
    "MasterChannelBindingError",
    "MasterChannelBindingErrorCode",
    "MasterChannelBindingRecord",
    "ResolveMasterBindingOutcome",
    "ResolveMasterBindingResult",
    "BindMasterBindingOutcome",
    "BindMasterBindingResult",
    "RebindMasterBindingOutcome",
    "RebindMasterBindingResult",
    "RevokeMasterBindingOutcome",
    "RevokeMasterBindingResult",
    "normalize_connection_scope",
    "normalize_external_account_id",
    "require_master_binding_channel",
    "require_canonical_master_id",
)

DEFAULT_CONNECTION_SCOPE: Final[str] = "default"
EXTERNAL_ACCOUNT_ID_MAX_LENGTH: Final[int] = 128
CONNECTION_SCOPE_MAX_LENGTH: Final[int] = 128

# Opaque channel account id: no whitespace/control; no case folding (avoids
# false merges). Printable ASCII excluding space and DEL.
_OPAQUE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]+$")
_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class MasterBindingChannel(str, enum.Enum):
    """Binding channels. Values reserved for future adapters; none live-wired."""

    SYNTHETIC = "synthetic"
    VK = "vk"
    MAX = "max"


class MasterBindingStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class MasterChannelBindingErrorCode(str, enum.Enum):
    """Fixed technical codes. Never embed identities, masterId, or free text.

    ``REVOKED`` is reserved for exception-raising call sites that need to name
    a revoked row explicitly. Soft resolve/revoke APIs surface revoked rows as
    ``NOT_FOUND`` / ``REVOKED`` outcomes instead of raising this code.
    """

    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    AMBIGUOUS = "AMBIGUOUS"
    REVOKED = "REVOKED"  # reserved; not used as a soft service outcome today
    ALREADY_BOUND = "ALREADY_BOUND"


class MasterChannelBindingError(RuntimeError):
    """Fail-closed binding error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str:
            if isinstance(code, MasterChannelBindingErrorCode):
                code = code.value
            else:
                super().__init__(MasterChannelBindingErrorCode.INVALID_INPUT.value)
                return
        if code not in {item.value for item in MasterChannelBindingErrorCode}:
            super().__init__(MasterChannelBindingErrorCode.INVALID_INPUT.value)
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return (
            str(self.args[0])
            if self.args
            else MasterChannelBindingErrorCode.INVALID_INPUT.value
        )

    def __repr__(self) -> str:
        return f"MasterChannelBindingError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_master_binding_channel(value: object) -> MasterBindingChannel:
    if type(value) is MasterBindingChannel:
        return value
    if type(value) is not str or not value:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    try:
        return MasterBindingChannel(value)
    except ValueError:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None


def normalize_connection_scope(value: object) -> str:
    """Strict opaque scope. No trim-normalize of internals; rejects whitespace."""

    if type(value) is not str or not value:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if len(value) > CONNECTION_SCOPE_MAX_LENGTH:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    return value


def normalize_external_account_id(value: object) -> str:
    """Strict opaque account id. No case folding (prevents false coincidences)."""

    if type(value) is not str or not value:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if len(value) > EXTERNAL_ACCOUNT_ID_MAX_LENGTH:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    return value


def require_canonical_master_id(value: object) -> str:
    """Require online-zapis-tv masterId as canonical lowercase UUID. Never echoes."""

    if type(value) is not str or not value:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if value != value.lower():
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if value.startswith("{") or value.lower().startswith("urn:"):
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    if _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    canonical = str(parsed)
    if canonical != value:
        raise MasterChannelBindingError(
            MasterChannelBindingErrorCode.INVALID_INPUT
        ) from None
    return canonical


@dataclass(frozen=True, slots=True, repr=False)
class MasterChannelBindingRecord:
    """Safe projection of a durable binding row."""

    binding_id: uuid.UUID
    channel: MasterBindingChannel
    connection_scope: str
    external_account_id: str
    master_id: str
    status: MasterBindingStatus
    bound_at: datetime
    revoked_at: datetime | None

    def __repr__(self) -> str:
        return (
            "MasterChannelBindingRecord("
            "binding_id=<redacted>, "
            f"channel={self.channel.value!r}, "
            "connection_scope=<redacted>, "
            "external_account_id=<redacted>, "
            "master_id=<redacted>, "
            f"status={self.status.value!r}, "
            "bound_at=<redacted>, "
            f"revoked_at={'set' if self.revoked_at is not None else None})"
        )


class ResolveMasterBindingOutcome(str, enum.Enum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class ResolveMasterBindingResult:
    outcome: ResolveMasterBindingOutcome
    master_id: str | None = None
    binding: MasterChannelBindingRecord | None = None

    def __post_init__(self) -> None:
        if self.outcome is ResolveMasterBindingOutcome.RESOLVED:
            if type(self.master_id) is not str or not self.master_id:
                raise TypeError("RESOLVED requires master_id") from None
            if self.binding is None:
                raise TypeError("RESOLVED requires binding") from None
        elif self.master_id is not None or self.binding is not None:
            raise TypeError("non-RESOLVED must not carry master_id/binding") from None

    def __repr__(self) -> str:
        return (
            "ResolveMasterBindingResult("
            f"outcome={self.outcome.value!r}, "
            "master_id=<redacted>, "
            f"binding={'set' if self.binding is not None else None})"
        )


class BindMasterBindingOutcome(str, enum.Enum):
    BOUND = "BOUND"
    ALREADY_BOUND = "ALREADY_BOUND"
    CONFLICT = "CONFLICT"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class BindMasterBindingResult:
    outcome: BindMasterBindingOutcome
    binding: MasterChannelBindingRecord | None = None

    def __post_init__(self) -> None:
        if self.outcome in (
            BindMasterBindingOutcome.BOUND,
            BindMasterBindingOutcome.ALREADY_BOUND,
        ):
            if self.binding is None:
                raise TypeError(f"{self.outcome.value} requires binding") from None
        elif self.binding is not None:
            raise TypeError("failure outcome must not carry binding") from None

    def __repr__(self) -> str:
        return (
            "BindMasterBindingResult("
            f"outcome={self.outcome.value!r}, "
            f"binding={'set' if self.binding is not None else None})"
        )


class RebindMasterBindingOutcome(str, enum.Enum):
    REBOUND = "REBOUND"
    BOUND = "BOUND"
    ALREADY_BOUND = "ALREADY_BOUND"
    CONFLICT = "CONFLICT"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class RebindMasterBindingResult:
    outcome: RebindMasterBindingOutcome
    binding: MasterChannelBindingRecord | None = None
    revoked_binding_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.outcome in (
            RebindMasterBindingOutcome.REBOUND,
            RebindMasterBindingOutcome.BOUND,
            RebindMasterBindingOutcome.ALREADY_BOUND,
        ):
            if self.binding is None:
                raise TypeError(f"{self.outcome.value} requires binding") from None
        elif self.binding is not None or self.revoked_binding_id is not None:
            raise TypeError("failure outcome must not carry binding ids") from None

    def __repr__(self) -> str:
        return (
            "RebindMasterBindingResult("
            f"outcome={self.outcome.value!r}, "
            f"binding={'set' if self.binding is not None else None}, "
            "revoked_binding_id="
            f"{'set' if self.revoked_binding_id is not None else None})"
        )


class RevokeMasterBindingOutcome(str, enum.Enum):
    REVOKED = "REVOKED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class RevokeMasterBindingResult:
    outcome: RevokeMasterBindingOutcome
    binding: MasterChannelBindingRecord | None = None

    def __post_init__(self) -> None:
        if self.outcome is RevokeMasterBindingOutcome.REVOKED:
            if self.binding is None:
                raise TypeError("REVOKED requires binding") from None
            if self.binding.status is not MasterBindingStatus.REVOKED:
                raise TypeError("REVOKED result must carry revoked status") from None
        elif self.binding is not None:
            raise TypeError("non-REVOKED must not carry binding") from None

    def __repr__(self) -> str:
        return (
            "RevokeMasterBindingResult("
            f"outcome={self.outcome.value!r}, "
            f"binding={'set' if self.binding is not None else None})"
        )
