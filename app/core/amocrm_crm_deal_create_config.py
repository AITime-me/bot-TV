"""Fail-closed amoCRM technical-deal creation gate (AMO-01B2).

Separate from Chat HMAC. Requires CRM REST + pipeline/status when enabled.
No hardcoded account IDs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.amocrm_crm_rest_config import (
    AmoCrmCrmRestConfig,
    AmoCrmCrmRestConfigError,
    load_crm_rest_config_fail_closed,
)

__all__ = (
    "AmoCrmDealCreateConfig",
    "AmoCrmDealCreateConfigError",
    "load_deal_create_config_fail_closed",
)


class AmoCrmDealCreateConfigError(ValueError):
    def __init__(self, code: str = "AMOCRM_CRM_DEAL_CREATE_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmDealCreateConfigError({self.args[0]!r})"


def _require_positive_int(raw: object, *, code: str) -> int:
    if type(raw) is not str or not raw:
        raise AmoCrmDealCreateConfigError(code) from None
    if any(ch.isspace() for ch in raw) or not raw.isdigit():
        raise AmoCrmDealCreateConfigError(code) from None
    value = int(raw)
    if value <= 0:
        raise AmoCrmDealCreateConfigError(code) from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmDealCreateConfig:
    enabled: bool = False
    pipeline_id: int | None = None
    status_id: int | None = None
    rest: AmoCrmCrmRestConfig | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmDealCreateConfig("
            f"enabled={self.enabled!r}, "
            f"pipeline_id={self.pipeline_id!r}, "
            f"status_id={self.status_id!r}, "
            "rest=<redacted>)"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmDealCreateConfigError("AMOCRM_CRM_DEAL_CREATE_DISABLED") from None
        if self.pipeline_id is None or self.status_id is None:
            raise AmoCrmDealCreateConfigError(
                "AMOCRM_CRM_DEAL_CREATE_CONFIG_INVALID"
            ) from None
        if self.rest is None:
            raise AmoCrmDealCreateConfigError(
                "AMOCRM_CRM_DEAL_CREATE_CONFIG_INVALID"
            ) from None
        self.rest.require_runtime()

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AmoCrmDealCreateConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("AMOCRM_CRM_DEAL_CREATE_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False)
        if enabled_raw != "true":
            raise AmoCrmDealCreateConfigError(
                "AMOCRM_CRM_DEAL_CREATE_CONFIG_INVALID"
            ) from None

        try:
            rest = AmoCrmCrmRestConfig.from_env(source)
        except AmoCrmCrmRestConfigError as exc:
            raise AmoCrmDealCreateConfigError(str(exc.args[0])) from None
        if not rest.enabled:
            raise AmoCrmDealCreateConfigError("AMOCRM_CRM_REST_DISABLED") from None

        pipeline_raw = source.get("AMOCRM_CRM_DEAL_PIPELINE_ID")
        status_raw = source.get("AMOCRM_CRM_DEAL_STATUS_ID")
        if pipeline_raw is None or pipeline_raw == "":
            raise AmoCrmDealCreateConfigError(
                "AMOCRM_CRM_DEAL_PIPELINE_ID_REQUIRED"
            ) from None
        if status_raw is None or status_raw == "":
            raise AmoCrmDealCreateConfigError(
                "AMOCRM_CRM_DEAL_STATUS_ID_REQUIRED"
            ) from None
        pipeline_id = _require_positive_int(
            pipeline_raw,
            code="AMOCRM_CRM_DEAL_PIPELINE_ID_INVALID",
        )
        status_id = _require_positive_int(
            status_raw,
            code="AMOCRM_CRM_DEAL_STATUS_ID_INVALID",
        )
        return cls(
            enabled=True,
            pipeline_id=pipeline_id,
            status_id=status_id,
            rest=rest,
        )


def load_deal_create_config_fail_closed(
    environ: Mapping[str, str] | None = None,
) -> AmoCrmDealCreateConfig:
    try:
        return AmoCrmDealCreateConfig.from_env(environ)
    except AmoCrmDealCreateConfigError:
        return AmoCrmDealCreateConfig(enabled=False)
    except AmoCrmCrmRestConfigError:
        return AmoCrmDealCreateConfig(enabled=False)
