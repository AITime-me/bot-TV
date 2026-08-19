"""Identity Resolution types and validators (CURSOR-30).

Canonical client identity is a stable bot-TV UUID. External identities/entities
link through a durable graph. Matching never uses name/free text. No live
amoCRM / VK / MAX / n8n I/O.
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
    "EXTERNAL_ID_MAX_LENGTH",
    "CONNECTION_SCOPE_MAX_LENGTH",
    "PROVIDER_MAX_LENGTH",
    "SOURCE_MAX_LENGTH",
    "IdentityEntityKind",
    "IdentityLinkStatus",
    "IdentityLinkConfidence",
    "CanonicalIdentityStatus",
    "IdentityResolutionError",
    "IdentityResolutionErrorCode",
    "IdentityLinkRecord",
    "CanonicalIdentityRecord",
    "CanonicalIdentityGraph",
    "IdentityResolveSignals",
    "ResolveIdentityOutcome",
    "ResolveIdentityResult",
    "AttachIdentityLinkOutcome",
    "AttachIdentityLinkResult",
    "RevokeIdentityLinkOutcome",
    "RevokeIdentityLinkResult",
    "InspectIdentityOutcome",
    "InspectIdentityResult",
    "ReconcileBuyerCardOutcome",
    "ReconcileBuyerCardResult",
    "normalize_connection_scope",
    "normalize_provider",
    "normalize_external_id",
    "normalize_phone_e164",
    "normalize_email",
    "require_canonical_identity_id",
    "require_entity_kind",
    "require_link_confidence",
    "require_link_source",
    "PHONE_PROVIDER",
    "EMAIL_PROVIDER",
    "REASON_EMAIL_ONLY_SECONDARY",
    "REASON_DEAL_TECH_ROLE_CONFLICT",
    "AMOCRM_LEAD_ROLE_ENTITY_KINDS",
    "AMOCRM_DEAL_ENTITY_KINDS",
)

DEFAULT_CONNECTION_SCOPE: Final[str] = "default"
EXTERNAL_ID_MAX_LENGTH: Final[int] = 256
CONNECTION_SCOPE_MAX_LENGTH: Final[int] = 128
PROVIDER_MAX_LENGTH: Final[int] = 64
SOURCE_MAX_LENGTH: Final[int] = 64

# Opaque provider / scope / external ids: printable ASCII excluding space/DEL.
_OPAQUE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]+$")
_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# E.164: + and 10–15 digits; no national guessing beyond deterministic RU 8→7.
_E164_RE: Final[re.Pattern[str]] = re.compile(r"^\+[1-9]\d{9,14}$")
# Conservative email: local@domain with at least one dot in domain; no spaces.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9])?@"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

PHONE_PROVIDER: Final[str] = "phone"
EMAIL_PROVIDER: Final[str] = "email"

# Safe resolve reason when email matched but no primary (a–c) candidate exists.
REASON_EMAIL_ONLY_SECONDARY: Final[str] = "EMAIL_ONLY_SECONDARY"

# Safe attach reason when a business Lead and a technical/chat Lead share an id.
REASON_DEAL_TECH_ROLE_CONFLICT: Final[str] = "deal_technical_deal_conflict"


class IdentityEntityKind(str, enum.Enum):
    """Extensible closed set of entity kinds (new kinds need a migration)."""

    CHANNEL_ACCOUNT = "CHANNEL_ACCOUNT"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    ONLINE_ZAPIS_CLIENT = "ONLINE_ZAPIS_CLIENT"
    AMOCRM_CONTACT = "AMOCRM_CONTACT"
    AMOCRM_BUYER_CARD = "AMOCRM_BUYER_CARD"
    AMOCRM_TECHNICAL_DEAL = "AMOCRM_TECHNICAL_DEAL"
    AMOCRM_DEAL = "AMOCRM_DEAL"


# Lead-namespace roles: one Lead id cannot be ACTIVE in both roles at once.
# Buyer Card (Customer) is a different amoCRM namespace and is not included.
AMOCRM_LEAD_ROLE_ENTITY_KINDS: Final[frozenset[IdentityEntityKind]] = frozenset(
    {
        IdentityEntityKind.AMOCRM_DEAL,
        IdentityEntityKind.AMOCRM_TECHNICAL_DEAL,
    }
)
# Back-compat alias used by older comments/tests; Lead roles only.
AMOCRM_DEAL_ENTITY_KINDS: Final[frozenset[IdentityEntityKind]] = (
    AMOCRM_LEAD_ROLE_ENTITY_KINDS
)


class IdentityLinkStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class IdentityLinkConfidence(str, enum.Enum):
    """CONFIRMED participates in primary matching; SECONDARY is email-class."""

    CONFIRMED = "CONFIRMED"
    SECONDARY = "SECONDARY"


class CanonicalIdentityStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class IdentityResolutionErrorCode(str, enum.Enum):
    """Fixed technical codes. Never embed PII, external ids, or free text."""

    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    ALREADY_LINKED = "ALREADY_LINKED"
    REVOKED = "REVOKED"


class IdentityResolutionError(RuntimeError):
    """Fail-closed identity error. Message is a fixed code only."""

    def __init__(self, code: object) -> None:
        if type(code) is not str:
            if isinstance(code, IdentityResolutionErrorCode):
                code = code.value
            else:
                super().__init__(IdentityResolutionErrorCode.INVALID_INPUT.value)
                return
        if code not in {item.value for item in IdentityResolutionErrorCode}:
            super().__init__(IdentityResolutionErrorCode.INVALID_INPUT.value)
            return
        super().__init__(code)

    @property
    def code(self) -> str:
        return (
            str(self.args[0])
            if self.args
            else IdentityResolutionErrorCode.INVALID_INPUT.value
        )

    def __repr__(self) -> str:
        return f"IdentityResolutionError({self.code!r})"

    def __str__(self) -> str:
        return self.code


def _contains_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def require_entity_kind(value: object) -> IdentityEntityKind:
    if type(value) is IdentityEntityKind:
        return value
    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    try:
        return IdentityEntityKind(value)
    except ValueError:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None


def require_link_confidence(value: object) -> IdentityLinkConfidence:
    if type(value) is IdentityLinkConfidence:
        return value
    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    try:
        return IdentityLinkConfidence(value)
    except ValueError:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None


def require_link_source(value: object) -> str:
    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(value) > SOURCE_MAX_LENGTH:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return value


def normalize_provider(value: object) -> str:
    """Provider/channel token. Extensible; no case folding; printable ASCII."""

    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(value) > PROVIDER_MAX_LENGTH:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return value


def normalize_connection_scope(value: object) -> str:
    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(value) > CONNECTION_SCOPE_MAX_LENGTH:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return value


def normalize_external_id(value: object) -> str:
    """Opaque external id. No case folding (except callers that pre-normalize)."""

    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(value) > EXTERNAL_ID_MAX_LENGTH:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return value


def normalize_phone_e164(value: object) -> str:
    """Deterministic phone → E.164. Never guesses incomplete/invalid input.

    Accepts already-canonical ``+…`` or digit strings with optional leading
    ``+``. RU national ``8XXXXXXXXXX`` maps to ``+7…``; bare 10-digit national
    maps to ``+7…``. Shorter/longer/ambiguous forms fail closed.
    """

    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    stripped = value.strip()
    if not stripped:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    # Allow common separators; reject letters/other junk. Digits must be
    # recoverable without guessing missing country/national parts.
    digits = re.sub(r"[^\d]", "", stripped)
    if not digits:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    allowed_noise = set("+-(). ")
    for ch in stripped:
        if ch.isdigit() or ch in allowed_noise:
            continue
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    candidate = f"+{digits}"
    if _E164_RE.fullmatch(candidate) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return candidate


def normalize_email(value: object) -> str:
    """Conservative email normalize: trim + lowercase; no provider-specific merge."""

    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    stripped = value.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if len(stripped) > EXTERNAL_ID_MAX_LENGTH:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    normalized = stripped.lower()
    if _EMAIL_RE.fullmatch(normalized) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    # Must also satisfy opaque external_id printable-ASCII storage.
    if _OPAQUE_ID_RE.fullmatch(normalized) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return normalized


def require_canonical_identity_id(value: object) -> str:
    """Require bot-TV canonical identity as lowercase UUID string. Never echoes."""

    if type(value) is uuid.UUID:
        canonical = str(value)
        if _CANONICAL_UUID_RE.fullmatch(canonical) is None:
            raise IdentityResolutionError(
                IdentityResolutionErrorCode.INVALID_INPUT
            ) from None
        return canonical
    if type(value) is not str or not value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if any(ch.isspace() for ch in value) or _contains_control_chars(value):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if value != value.lower():
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if value.startswith("{") or value.lower().startswith("urn:"):
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    if _CANONICAL_UUID_RE.fullmatch(value) is None:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    canonical = str(parsed)
    if canonical != value:
        raise IdentityResolutionError(
            IdentityResolutionErrorCode.INVALID_INPUT
        ) from None
    return canonical


@dataclass(frozen=True, slots=True, repr=False)
class IdentityLinkRecord:
    """Safe projection of one durable external link row."""

    link_id: uuid.UUID
    canonical_identity_id: uuid.UUID
    provider: str
    connection_scope: str
    entity_kind: IdentityEntityKind
    external_id: str
    status: IdentityLinkStatus
    confidence: IdentityLinkConfidence
    source: str
    linked_at: datetime
    revoked_at: datetime | None

    def __repr__(self) -> str:
        return (
            "IdentityLinkRecord("
            "link_id=<redacted>, "
            "canonical_identity_id=<redacted>, "
            f"provider={self.provider!r}, "
            "connection_scope=<redacted>, "
            f"entity_kind={self.entity_kind.value!r}, "
            "external_id=<redacted>, "
            f"status={self.status.value!r}, "
            f"confidence={self.confidence.value!r}, "
            f"source={self.source!r}, "
            "linked_at=<redacted>, "
            f"revoked_at={'set' if self.revoked_at is not None else None})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalIdentityRecord:
    identity_id: uuid.UUID
    status: CanonicalIdentityStatus
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        return (
            "CanonicalIdentityRecord("
            "identity_id=<redacted>, "
            f"status={self.status.value!r}, "
            "created_at=<redacted>, "
            "updated_at=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CanonicalIdentityGraph:
    identity: CanonicalIdentityRecord
    links: tuple[IdentityLinkRecord, ...]

    def __repr__(self) -> str:
        return (
            "CanonicalIdentityGraph("
            f"identity={self.identity!r}, "
            f"links_count={len(self.links)})"
        )

    def active_external_ids_by_kind(
        self,
    ) -> dict[IdentityEntityKind, tuple[str, ...]]:
        out: dict[IdentityEntityKind, list[str]] = {}
        for link in self.links:
            if link.status is not IdentityLinkStatus.ACTIVE:
                continue
            out.setdefault(link.entity_kind, []).append(link.external_id)
        return {kind: tuple(ids) for kind, ids in out.items()}


@dataclass(frozen=True, slots=True, repr=False)
class IdentityResolveSignals:
    """Resolution inputs. Name/free-text fields are intentionally absent."""

    channel_provider: str | None = None
    channel_connection_scope: str | None = None
    channel_external_account_id: str | None = None
    phone: str | None = None
    email: str | None = None
    # Additional confirmed durable lookups (provider, scope, kind, external_id).
    confirmed_links: tuple[
        tuple[str, str, IdentityEntityKind | str, str], ...
    ] = ()

    def __repr__(self) -> str:
        return (
            "IdentityResolveSignals("
            f"channel_provider={self.channel_provider!r}, "
            "channel_connection_scope=<redacted>, "
            "channel_external_account_id=<redacted>, "
            "phone=<redacted>, "
            "email=<redacted>, "
            f"confirmed_links_count={len(self.confirmed_links)})"
        )


class ResolveIdentityOutcome(str, enum.Enum):
    RESOLVED = "RESOLVED"
    NOT_FOUND = "NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class ResolveIdentityResult:
    outcome: ResolveIdentityOutcome
    canonical_identity_id: uuid.UUID | None = None
    confidence: IdentityLinkConfidence | None = None
    reason: str | None = None
    known_external_ids: tuple[IdentityLinkRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is ResolveIdentityOutcome.RESOLVED:
            if self.canonical_identity_id is None:
                raise TypeError("RESOLVED requires canonical_identity_id") from None
            if self.confidence is None:
                raise TypeError("RESOLVED requires confidence") from None
            if type(self.reason) is not str or not self.reason:
                raise TypeError("RESOLVED requires reason") from None
        elif self.outcome is ResolveIdentityOutcome.MANUAL_REVIEW_REQUIRED:
            if type(self.reason) is not str or not self.reason:
                raise TypeError("MANUAL_REVIEW_REQUIRED requires reason") from None
            if self.canonical_identity_id is not None:
                raise TypeError(
                    "MANUAL_REVIEW_REQUIRED must not carry canonical id"
                ) from None
        elif self.outcome is ResolveIdentityOutcome.NOT_FOUND:
            if self.canonical_identity_id is not None or self.confidence is not None:
                raise TypeError("NOT_FOUND must not carry identity") from None
            # Optional safe reason (e.g. EMAIL_ONLY_SECONDARY); never PII.
        elif self.canonical_identity_id is not None or self.confidence is not None:
            raise TypeError("failure outcome must not carry identity") from None

    def __repr__(self) -> str:
        return (
            "ResolveIdentityResult("
            f"outcome={self.outcome.value!r}, "
            "canonical_identity_id=<redacted>, "
            f"confidence="
            f"{self.confidence.value if self.confidence is not None else None!r}, "
            f"reason={self.reason!r}, "
            f"known_external_ids_count={len(self.known_external_ids)})"
        )


class AttachIdentityLinkOutcome(str, enum.Enum):
    LINKED = "LINKED"
    ALREADY_LINKED = "ALREADY_LINKED"
    CREATED = "CREATED"
    CONFLICT = "CONFLICT"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class AttachIdentityLinkResult:
    outcome: AttachIdentityLinkOutcome
    canonical_identity_id: uuid.UUID | None = None
    link: IdentityLinkRecord | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome in (
            AttachIdentityLinkOutcome.LINKED,
            AttachIdentityLinkOutcome.ALREADY_LINKED,
            AttachIdentityLinkOutcome.CREATED,
        ):
            if self.canonical_identity_id is None or self.link is None:
                raise TypeError(f"{self.outcome.value} requires identity+link") from None
        elif self.outcome is AttachIdentityLinkOutcome.MANUAL_REVIEW_REQUIRED:
            if type(self.reason) is not str or not self.reason:
                raise TypeError("MANUAL_REVIEW_REQUIRED requires reason") from None
            if self.canonical_identity_id is not None or self.link is not None:
                raise TypeError("MANUAL_REVIEW must not carry link") from None
        elif self.link is not None or self.canonical_identity_id is not None:
            raise TypeError("failure outcome must not carry identity/link") from None

    def __repr__(self) -> str:
        return (
            "AttachIdentityLinkResult("
            f"outcome={self.outcome.value!r}, "
            "canonical_identity_id=<redacted>, "
            f"link={'set' if self.link is not None else None}, "
            f"reason={self.reason!r})"
        )


class RevokeIdentityLinkOutcome(str, enum.Enum):
    REVOKED = "REVOKED"
    NOT_FOUND = "NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class RevokeIdentityLinkResult:
    outcome: RevokeIdentityLinkOutcome
    link: IdentityLinkRecord | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is RevokeIdentityLinkOutcome.REVOKED:
            if self.link is None:
                raise TypeError("REVOKED requires link") from None
            if self.link.status is not IdentityLinkStatus.REVOKED:
                raise TypeError("REVOKED result must carry revoked status") from None
        elif self.outcome is RevokeIdentityLinkOutcome.MANUAL_REVIEW_REQUIRED:
            if type(self.reason) is not str or not self.reason:
                raise TypeError("MANUAL_REVIEW_REQUIRED requires reason") from None
            if self.link is not None:
                raise TypeError("MANUAL_REVIEW must not carry link") from None
        elif self.link is not None:
            raise TypeError("non-REVOKED must not carry link") from None

    def __repr__(self) -> str:
        return (
            "RevokeIdentityLinkResult("
            f"outcome={self.outcome.value!r}, "
            f"link={'set' if self.link is not None else None}, "
            f"reason={self.reason!r})"
        )


class InspectIdentityOutcome(str, enum.Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class InspectIdentityResult:
    outcome: InspectIdentityOutcome
    graph: CanonicalIdentityGraph | None = None

    def __post_init__(self) -> None:
        if self.outcome is InspectIdentityOutcome.FOUND:
            if self.graph is None:
                raise TypeError("FOUND requires graph") from None
        elif self.graph is not None:
            raise TypeError("non-FOUND must not carry graph") from None

    def __repr__(self) -> str:
        return (
            "InspectIdentityResult("
            f"outcome={self.outcome.value!r}, "
            f"graph={'set' if self.graph is not None else None})"
        )


class ReconcileBuyerCardOutcome(str, enum.Enum):
    REUSED = "REUSED"
    NOT_FOUND = "NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True, repr=False)
class ReconcileBuyerCardResult:
    """Buyer Card reconciliation. Technical deals are never treated as Buyer Cards."""

    outcome: ReconcileBuyerCardOutcome
    canonical_identity_id: uuid.UUID | None = None
    buyer_card_external_id: str | None = None
    confidence: IdentityLinkConfidence | None = None
    reason: str | None = None
    known_external_ids: tuple[IdentityLinkRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome is ReconcileBuyerCardOutcome.REUSED:
            if self.canonical_identity_id is None:
                raise TypeError("REUSED requires canonical_identity_id") from None
            if type(self.buyer_card_external_id) is not str or not self.buyer_card_external_id:
                raise TypeError("REUSED requires buyer_card_external_id") from None
            if self.confidence is None:
                raise TypeError("REUSED requires confidence") from None
            if type(self.reason) is not str or not self.reason:
                raise TypeError("REUSED requires reason") from None
        elif self.outcome is ReconcileBuyerCardOutcome.MANUAL_REVIEW_REQUIRED:
            if type(self.reason) is not str or not self.reason:
                raise TypeError("MANUAL_REVIEW_REQUIRED requires reason") from None
            if self.buyer_card_external_id is not None:
                raise TypeError("MANUAL_REVIEW must not pick a buyer card") from None
        elif self.buyer_card_external_id is not None:
            raise TypeError("failure must not carry buyer_card_external_id") from None

    def __repr__(self) -> str:
        return (
            "ReconcileBuyerCardResult("
            f"outcome={self.outcome.value!r}, "
            "canonical_identity_id=<redacted>, "
            "buyer_card_external_id=<redacted>, "
            f"confidence="
            f"{self.confidence.value if self.confidence is not None else None!r}, "
            f"reason={self.reason!r}, "
            f"known_external_ids_count={len(self.known_external_ids)})"
        )
