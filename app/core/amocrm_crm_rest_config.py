"""amoCRM CRM REST OAuth gate (AMO-01B2 foundation).

Default-off. Completely separate from AMOCRM_CHAT_* Chat HMAC auth.
No contact/deal/note/task creation in this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = (
    "AmoCrmCrmRestConfig",
    "AmoCrmCrmRestConfigError",
    "DEFAULT_AMOCRM_CRM_API_BASE_URL",
    "connection_scope_from_env",
    "load_crm_rest_config_fail_closed",
)

DEFAULT_AMOCRM_CRM_API_BASE_URL: Final[str] = "https://example.amocrm.ru"
_SECRET_MIN: Final[int] = 8
_SECRET_MAX: Final[int] = 256
_ID_MIN: Final[int] = 1
_ID_MAX: Final[int] = 128


class AmoCrmCrmRestConfigError(ValueError):
    def __init__(self, code: str = "AMOCRM_CRM_REST_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmCrmRestConfigError({self.args[0]!r})"


def _require_nonempty_token(value: str, *, code: str, min_len: int, max_len: int) -> str:
    if type(value) is not str or not value:
        raise AmoCrmCrmRestConfigError(code) from None
    if len(value) < min_len or len(value) > max_len:
        raise AmoCrmCrmRestConfigError(code) from None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AmoCrmCrmRestConfigError(code) from None
    if any(ch.isspace() for ch in value):
        raise AmoCrmCrmRestConfigError(code) from None
    return value


def _require_base_url(value: str) -> str:
    if type(value) is not str or not value:
        raise AmoCrmCrmRestConfigError("AMOCRM_CRM_API_BASE_INVALID") from None
    if any(ch.isspace() for ch in value) or any(ord(ch) < 32 for ch in value):
        raise AmoCrmCrmRestConfigError("AMOCRM_CRM_API_BASE_INVALID") from None
    if not value.startswith("https://"):
        raise AmoCrmCrmRestConfigError("AMOCRM_CRM_API_BASE_INVALID") from None
    return value.rstrip("/")


def connection_scope_from_env(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve ``AMOCRM_CRM_CONNECTION_SCOPE`` independently of REST enabled.

    Missing → ``default``. Invalid explicit value fails closed
    (``AMOCRM_CRM_CONNECTION_SCOPE_INVALID``).
    """

    source = os.environ if environ is None else environ
    scope_raw = source.get("AMOCRM_CRM_CONNECTION_SCOPE", "default")
    return _require_nonempty_token(
        scope_raw,
        code="AMOCRM_CRM_CONNECTION_SCOPE_INVALID",
        min_len=1,
        max_len=64,
    )


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmCrmRestConfig:
    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = None
    api_base_url: str = DEFAULT_AMOCRM_CRM_API_BASE_URL
    connection_scope: str = "default"

    def __repr__(self) -> str:
        return (
            "AmoCrmCrmRestConfig("
            f"enabled={self.enabled!r}, "
            "client_id=<redacted>, client_secret=<redacted>, "
            f"api_base_url={self.api_base_url!r}, "
            "connection_scope=<redacted>)"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmCrmRestConfigError("AMOCRM_CRM_REST_DISABLED") from None
        if self.client_id is None or self.client_secret is None:
            raise AmoCrmCrmRestConfigError("AMOCRM_CRM_REST_CONFIG_INVALID") from None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AmoCrmCrmRestConfig:
        source = os.environ if environ is None else environ
        connection_scope = connection_scope_from_env(source)
        enabled_raw = source.get("AMOCRM_CRM_REST_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False, connection_scope=connection_scope)
        if enabled_raw != "true":
            raise AmoCrmCrmRestConfigError("AMOCRM_CRM_REST_CONFIG_INVALID") from None

        client_id_raw = source.get("AMOCRM_CLIENT_ID")
        if client_id_raw is None or client_id_raw == "":
            raise AmoCrmCrmRestConfigError("AMOCRM_CRM_CLIENT_ID_REQUIRED") from None
        client_id = _require_nonempty_token(
            client_id_raw,
            code="AMOCRM_CRM_CLIENT_ID_INVALID",
            min_len=_ID_MIN,
            max_len=_ID_MAX,
        )

        secret_raw = source.get("AMOCRM_CLIENT_SECRET")
        if secret_raw is None or secret_raw == "":
            raise AmoCrmCrmRestConfigError("AMOCRM_CRM_CLIENT_SECRET_REQUIRED") from None
        client_secret = _require_nonempty_token(
            secret_raw,
            code="AMOCRM_CRM_CLIENT_SECRET_INVALID",
            min_len=_SECRET_MIN,
            max_len=_SECRET_MAX,
        )

        base_raw = source.get(
            "AMOCRM_CRM_API_BASE_URL",
            source.get("AMOCRM_BASE_URL", DEFAULT_AMOCRM_CRM_API_BASE_URL),
        )
        api_base_url = _require_base_url(base_raw)
        return cls(
            enabled=True,
            client_id=client_id,
            client_secret=client_secret,
            api_base_url=api_base_url,
            connection_scope=connection_scope,
        )


def load_crm_rest_config_fail_closed(
    environ: Mapping[str, str] | None = None,
) -> AmoCrmCrmRestConfig:
    source = os.environ if environ is None else environ
    try:
        return AmoCrmCrmRestConfig.from_env(environ)
    except AmoCrmCrmRestConfigError as exc:
        code = str(exc.args[0]) if exc.args else ""
        # Invalid explicit scope must not be silently rewritten to default.
        if code == "AMOCRM_CRM_CONNECTION_SCOPE_INVALID":
            raise
        try:
            scope = connection_scope_from_env(source)
        except AmoCrmCrmRestConfigError:
            scope = "default"
        return AmoCrmCrmRestConfig(enabled=False, connection_scope=scope)
