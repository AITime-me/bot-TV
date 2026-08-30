"""Strict immutable control-plane publication contracts (schemaVersion=1).

Checksum is treated as an opaque verified publication identity from online-zapis.
No local checksum re-canonicalization is performed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Final, Mapping, NoReturn

from app.core.control_plane_remote import CONTROL_PLANE_SCHEMA_VERSION

_SHA256_HEX_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
# Must stay in lockstep with online-zapis-tv
# ``src/lib/bot-knowledge/stable-key.ts`` ``BOT_KNOWLEDGE_STABLE_KEY_RE``:
# ``/^[a-z0-9]+(?:[._-][a-z0-9]+)*$/`` — hierarchical keys
# (``procedure.celosom``, ``procedure.pm_general``) and hyphenated
# (``faq-general``). Leading/trailing/empty separators fail closed.
BOT_KNOWLEDGE_STABLE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$"
)
_STABLE_KEY_RE: Final[re.Pattern[str]] = BOT_KNOWLEDGE_STABLE_KEY_RE

_MAX_PUBLICATION_ID_LEN: Final[int] = 64
_MAX_CONTENT_POLICY_CHARS: Final[int] = 100_000
_MAX_STABLE_KEY: Final[int] = 120
_MAX_TITLE: Final[int] = 200
_MAX_CONTENT: Final[int] = 20_000
_MAX_TAG: Final[int] = 64
_MAX_TAGS: Final[int] = 20
_MAX_ENTRIES: Final[int] = 500

_SETTINGS_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "schemaVersion",
        "publicationId",
        "version",
        "checksum",
        "publishedAt",
        "sourceUpdatedAt",
        "settings",
    }
)
_SETTINGS_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schemaVersion",
        "desiredAdminState",
        "provider",
        "channels",
        "contentPolicy",
        "limits",
        "operationalSafety",
    }
)
_DESIRED_ADMIN_KEYS: Final[frozenset[str]] = frozenset(
    {"isEnabled", "mode", "responseMode"}
)
_CHANNEL_KEYS: Final[frozenset[str]] = frozenset(
    {"siteWidget", "vk", "max", "telegram", "whatsapp"}
)
_CONTENT_POLICY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "mainInstruction",
        "knowledgeBaseNote",
        "handoffRules",
        "taggingRules",
        "safetyRules",
    }
)
_LIMIT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "maxMessagesPerClient",
        "maxDailyMessages",
        "logRetentionDays",
        "errorLogRetentionDays",
        "maxStoredBotEvents",
    }
)
_OPERATIONAL_SAFETY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "emergencyLockOwnedByBotCoreEnv",
        "effectiveRuntimeModeOwnedByBotCoreEnv",
    }
)
_KNOWLEDGE_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "ok",
        "schemaVersion",
        "knowledgePublicationId",
        "version",
        "checksum",
        "publishedAt",
        "entries",
    }
)
_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {"key", "category", "title", "content", "tags", "serviceId"}
)

_CP_MODES: Final[frozenset[str]] = frozenset(
    {"OFF", "TEST", "HINTS", "DRAFT", "AUTO"}
)
_CP_RESPONSE_MODES: Final[frozenset[str]] = frozenset(
    {"HINTS", "DRAFT", "AUTO"}
)
_CP_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"NONE", "YANDEX", "OPENAI", "MANUAL"}
)


class ControlPlaneSnapshotKind(StrEnum):
    SETTINGS = "SETTINGS"
    KNOWLEDGE = "KNOWLEDGE"


class KnowledgeCategory(StrEnum):
    PROCEDURE_EXPLANATION = "PROCEDURE_EXPLANATION"
    FAQ = "FAQ"
    PREPARATION = "PREPARATION"
    AFTERCARE = "AFTERCARE"
    OBJECTION_HANDLING = "OBJECTION_HANDLING"
    SAFETY_INFORMATION = "SAFETY_INFORMATION"
    POLICY_EXPLANATION = "POLICY_EXPLANATION"
    ESCALATION_GUIDANCE = "ESCALATION_GUIDANCE"


class ControlPlaneKindReadiness(StrEnum):
    READY_FRESH = "READY_FRESH"
    READY_STALE = "READY_STALE"
    NOT_READY = "NOT_READY"
    INVALID = "INVALID"
    AUTH_ERROR = "AUTH_ERROR"


class ControlPlaneOverallReadiness(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


class ControlPlaneParseError(ValueError):
    """Strict contract violation. Message is a fixed safe code only."""

    def __init__(self, code: str = "RESPONSE_INVALID") -> None:
        super().__init__(code)

    @property
    def code(self) -> str:
        return str(self.args[0]) if self.args else "RESPONSE_INVALID"

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"ControlPlaneParseError({self.code!r})"


def _fail(code: str = "RESPONSE_INVALID") -> NoReturn:
    raise ControlPlaneParseError(code) from None


def _require_mapping(value: object) -> Mapping[str, object]:
    if type(value) is not dict:
        _fail()
    return value


def _require_exact_keys(
    mapping: Mapping[str, object], allowed: frozenset[str]
) -> None:
    keys = frozenset(mapping.keys())
    if keys != allowed:
        _fail()


def _require_bool(value: object) -> bool:
    if type(value) is not bool:
        _fail()
    return value


def _require_positive_int(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 1:
        _fail()
    return value


def _require_schema_version(value: object) -> int:
    version = _require_positive_int(value)
    if version != CONTROL_PLANE_SCHEMA_VERSION:
        _fail()
    return version


def _require_checksum(value: object) -> str:
    if type(value) is not str or not _SHA256_HEX_RE.fullmatch(value):
        _fail()
    return value


def _require_publication_id(value: object) -> str:
    if type(value) is not str or not value:
        _fail()
    if len(value) > _MAX_PUBLICATION_ID_LEN:
        _fail()
    lowered = value.lower()
    if not _CANONICAL_UUID_RE.fullmatch(lowered):
        _fail()
    return lowered


def _require_iso_datetime(value: object) -> datetime:
    if type(value) is not str or not value:
        _fail()
    raw = value
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        _fail()
    if parsed.tzinfo is None:
        _fail()
    return parsed.astimezone(timezone.utc)


def _require_nullable_bounded_string(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _fail()
    if len(value) > _MAX_CONTENT_POLICY_CHARS:
        _fail()
    return value


def _require_enum(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        _fail()
    return value


@dataclass(frozen=True, slots=True)
class DesiredAdminState:
    is_enabled: bool
    mode: str
    response_mode: str


@dataclass(frozen=True, slots=True)
class ControlPlaneChannels:
    site_widget: bool
    vk: bool
    max: bool
    telegram: bool
    whatsapp: bool


@dataclass(frozen=True, slots=True)
class ContentPolicy:
    main_instruction: str | None
    knowledge_base_note: str | None
    handoff_rules: str | None
    tagging_rules: str | None
    safety_rules: str | None


@dataclass(frozen=True, slots=True)
class ControlPlaneLimits:
    max_messages_per_client: int
    max_daily_messages: int
    log_retention_days: int
    error_log_retention_days: int
    max_stored_bot_events: int


@dataclass(frozen=True, slots=True)
class OperationalSafety:
    emergency_lock_owned_by_bot_core_env: bool
    effective_runtime_mode_owned_by_bot_core_env: bool


@dataclass(frozen=True, slots=True)
class SettingsPayloadV1:
    schema_version: int
    desired_admin_state: DesiredAdminState
    provider: str
    channels: ControlPlaneChannels
    content_policy: ContentPolicy
    limits: ControlPlaneLimits
    operational_safety: OperationalSafety


@dataclass(frozen=True, slots=True)
class SettingsPublicationV1:
    schema_version: int
    publication_id: str
    version: int
    checksum: str
    published_at: datetime
    source_updated_at: datetime
    settings: SettingsPayloadV1

    @property
    def identity(self) -> tuple[str, str]:
        return (self.publication_id, self.checksum)


@dataclass(frozen=True, slots=True)
class KnowledgeEntryV1:
    key: str
    category: KnowledgeCategory
    title: str
    content: str
    tags: tuple[str, ...]
    service_id: str | None


@dataclass(frozen=True, slots=True)
class KnowledgePublicationV1:
    schema_version: int
    knowledge_publication_id: str
    version: int
    checksum: str
    published_at: datetime
    entries: tuple[KnowledgeEntryV1, ...]

    @property
    def identity(self) -> tuple[str, str]:
        return (self.knowledge_publication_id, self.checksum)


@dataclass(frozen=True, slots=True)
class PublicationIdentity:
    kind: ControlPlaneSnapshotKind
    publication_id: str
    version: int
    checksum: str
    schema_version: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class ControlPlaneKindState:
    kind: ControlPlaneSnapshotKind
    readiness: ControlPlaneKindReadiness
    usable: bool
    identity: PublicationIdentity | None
    error_code: str | None
    verified_at: datetime | None
    fetched_at: datetime | None
    stale_age_seconds: int | None
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class ControlPlaneSnapshotState:
    settings: ControlPlaneKindState
    knowledge: ControlPlaneKindState
    overall: ControlPlaneOverallReadiness
    last_successful_refresh_at: datetime | None
    error_code: str | None


def _parse_desired_admin(raw: object) -> DesiredAdminState:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _DESIRED_ADMIN_KEYS)
    return DesiredAdminState(
        is_enabled=_require_bool(mapping["isEnabled"]),
        mode=_require_enum(mapping["mode"], _CP_MODES),
        response_mode=_require_enum(mapping["responseMode"], _CP_RESPONSE_MODES),
    )


def _parse_channels(raw: object) -> ControlPlaneChannels:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _CHANNEL_KEYS)
    return ControlPlaneChannels(
        site_widget=_require_bool(mapping["siteWidget"]),
        vk=_require_bool(mapping["vk"]),
        max=_require_bool(mapping["max"]),
        telegram=_require_bool(mapping["telegram"]),
        whatsapp=_require_bool(mapping["whatsapp"]),
    )


def _parse_content_policy(raw: object) -> ContentPolicy:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _CONTENT_POLICY_KEYS)
    return ContentPolicy(
        main_instruction=_require_nullable_bounded_string(
            mapping["mainInstruction"]
        ),
        knowledge_base_note=_require_nullable_bounded_string(
            mapping["knowledgeBaseNote"]
        ),
        handoff_rules=_require_nullable_bounded_string(mapping["handoffRules"]),
        tagging_rules=_require_nullable_bounded_string(mapping["taggingRules"]),
        safety_rules=_require_nullable_bounded_string(mapping["safetyRules"]),
    )


def _parse_limits(raw: object) -> ControlPlaneLimits:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _LIMIT_KEYS)
    return ControlPlaneLimits(
        max_messages_per_client=_require_positive_int(
            mapping["maxMessagesPerClient"]
        ),
        max_daily_messages=_require_positive_int(mapping["maxDailyMessages"]),
        log_retention_days=_require_positive_int(mapping["logRetentionDays"]),
        error_log_retention_days=_require_positive_int(
            mapping["errorLogRetentionDays"]
        ),
        max_stored_bot_events=_require_positive_int(
            mapping["maxStoredBotEvents"]
        ),
    )


def _parse_operational_safety(raw: object) -> OperationalSafety:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _OPERATIONAL_SAFETY_KEYS)
    emergency = mapping["emergencyLockOwnedByBotCoreEnv"]
    effective = mapping["effectiveRuntimeModeOwnedByBotCoreEnv"]
    if emergency is not True or effective is not True:
        _fail()
    return OperationalSafety(
        emergency_lock_owned_by_bot_core_env=True,
        effective_runtime_mode_owned_by_bot_core_env=True,
    )


def parse_settings_payload_v1(raw: object) -> SettingsPayloadV1:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _SETTINGS_PAYLOAD_KEYS)
    return SettingsPayloadV1(
        schema_version=_require_schema_version(mapping["schemaVersion"]),
        desired_admin_state=_parse_desired_admin(mapping["desiredAdminState"]),
        provider=_require_enum(mapping["provider"], _CP_PROVIDERS),
        channels=_parse_channels(mapping["channels"]),
        content_policy=_parse_content_policy(mapping["contentPolicy"]),
        limits=_parse_limits(mapping["limits"]),
        operational_safety=_parse_operational_safety(
            mapping["operationalSafety"]
        ),
    )


def parse_settings_publication_v1(raw: object) -> SettingsPublicationV1:
    mapping = _require_mapping(raw)
    keys = frozenset(mapping.keys())
    if keys == _SETTINGS_ENVELOPE_KEYS:
        if mapping["ok"] is not True:
            _fail()
    elif keys == (_SETTINGS_ENVELOPE_KEYS - {"ok"}):
        pass
    else:
        _fail()
    return SettingsPublicationV1(
        schema_version=_require_schema_version(mapping["schemaVersion"]),
        publication_id=_require_publication_id(mapping["publicationId"]),
        version=_require_positive_int(mapping["version"]),
        checksum=_require_checksum(mapping["checksum"]),
        published_at=_require_iso_datetime(mapping["publishedAt"]),
        source_updated_at=_require_iso_datetime(mapping["sourceUpdatedAt"]),
        settings=parse_settings_payload_v1(mapping["settings"]),
    )


def _require_entry_key(value: object) -> str:
    if type(value) is not str or not value:
        _fail()
    if len(value) > _MAX_STABLE_KEY or not _STABLE_KEY_RE.fullmatch(value):
        _fail()
    return value


def _require_nonempty_bounded(value: object, maximum: int) -> str:
    if type(value) is not str:
        _fail()
    trimmed = value.strip()
    if not trimmed or len(value) > maximum or len(trimmed) > maximum:
        _fail()
    return value


def _require_tags(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_TAGS:
        _fail()
    tags: list[str] = []
    for item in value:
        if type(item) is not str:
            _fail()
        trimmed = item.strip()
        if not trimmed or len(item) > _MAX_TAG or len(trimmed) > _MAX_TAG:
            _fail()
        tags.append(item)
    return tuple(tags)


def _require_nullable_service_id(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        _fail()
    lowered = value.lower()
    if not _CANONICAL_UUID_RE.fullmatch(lowered):
        _fail()
    return lowered


def parse_knowledge_entry_v1(raw: object) -> KnowledgeEntryV1:
    mapping = _require_mapping(raw)
    _require_exact_keys(mapping, _ENTRY_KEYS)
    category_raw = mapping["category"]
    if type(category_raw) is not str:
        _fail()
    try:
        category = KnowledgeCategory(category_raw)
    except ValueError:
        _fail()
    return KnowledgeEntryV1(
        key=_require_entry_key(mapping["key"]),
        category=category,
        title=_require_nonempty_bounded(mapping["title"], _MAX_TITLE),
        content=_require_nonempty_bounded(mapping["content"], _MAX_CONTENT),
        tags=_require_tags(mapping["tags"]),
        service_id=_require_nullable_service_id(mapping["serviceId"]),
    )


def parse_knowledge_publication_v1(raw: object) -> KnowledgePublicationV1:
    mapping = _require_mapping(raw)
    keys = frozenset(mapping.keys())
    if keys == _KNOWLEDGE_ENVELOPE_KEYS:
        if mapping["ok"] is not True:
            _fail()
    elif keys == (_KNOWLEDGE_ENVELOPE_KEYS - {"ok"}):
        pass
    else:
        _fail()
    entries_raw = mapping["entries"]
    if type(entries_raw) is not list or len(entries_raw) > _MAX_ENTRIES:
        _fail()
    entries = tuple(parse_knowledge_entry_v1(item) for item in entries_raw)
    return KnowledgePublicationV1(
        schema_version=_require_schema_version(mapping["schemaVersion"]),
        knowledge_publication_id=_require_publication_id(
            mapping["knowledgePublicationId"]
        ),
        version=_require_positive_int(mapping["version"]),
        checksum=_require_checksum(mapping["checksum"]),
        published_at=_require_iso_datetime(mapping["publishedAt"]),
        entries=entries,
    )


def settings_publication_to_payload_dict(
    publication: SettingsPublicationV1,
) -> dict[str, object]:
    settings = publication.settings
    return {
        "schemaVersion": publication.schema_version,
        "publicationId": publication.publication_id,
        "version": publication.version,
        "checksum": publication.checksum,
        "publishedAt": publication.published_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "sourceUpdatedAt": publication.source_updated_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "settings": {
            "schemaVersion": settings.schema_version,
            "desiredAdminState": {
                "isEnabled": settings.desired_admin_state.is_enabled,
                "mode": settings.desired_admin_state.mode,
                "responseMode": settings.desired_admin_state.response_mode,
            },
            "provider": settings.provider,
            "channels": {
                "siteWidget": settings.channels.site_widget,
                "vk": settings.channels.vk,
                "max": settings.channels.max,
                "telegram": settings.channels.telegram,
                "whatsapp": settings.channels.whatsapp,
            },
            "contentPolicy": {
                "mainInstruction": settings.content_policy.main_instruction,
                "knowledgeBaseNote": settings.content_policy.knowledge_base_note,
                "handoffRules": settings.content_policy.handoff_rules,
                "taggingRules": settings.content_policy.tagging_rules,
                "safetyRules": settings.content_policy.safety_rules,
            },
            "limits": {
                "maxMessagesPerClient": settings.limits.max_messages_per_client,
                "maxDailyMessages": settings.limits.max_daily_messages,
                "logRetentionDays": settings.limits.log_retention_days,
                "errorLogRetentionDays": (
                    settings.limits.error_log_retention_days
                ),
                "maxStoredBotEvents": settings.limits.max_stored_bot_events,
            },
            "operationalSafety": {
                "emergencyLockOwnedByBotCoreEnv": True,
                "effectiveRuntimeModeOwnedByBotCoreEnv": True,
            },
        },
    }


def knowledge_publication_to_payload_dict(
    publication: KnowledgePublicationV1,
) -> dict[str, object]:
    return {
        "schemaVersion": publication.schema_version,
        "knowledgePublicationId": publication.knowledge_publication_id,
        "version": publication.version,
        "checksum": publication.checksum,
        "publishedAt": publication.published_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "entries": [
            {
                "key": entry.key,
                "category": entry.category.value,
                "title": entry.title,
                "content": entry.content,
                "tags": list(entry.tags),
                "serviceId": entry.service_id,
            }
            for entry in publication.entries
        ],
    }
