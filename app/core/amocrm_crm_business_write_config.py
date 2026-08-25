"""Fail-closed amoCRM business write config for Teya request orchestrator.

Business «Лиды» pipeline/status/manager/task IDs come from env — never from
hardcoded ControlledRevision PROGREV constants. Separate from technical-deal
create config (AMOCRM_CRM_DEAL_*).
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
    "AmoCrmBusinessWriteConfig",
    "AmoCrmBusinessWriteConfigError",
    "load_business_write_config_fail_closed",
)


class AmoCrmBusinessWriteConfigError(ValueError):
    def __init__(self, code: str = "AMOCRM_CRM_BUSINESS_WRITE_CONFIG_INVALID") -> None:
        super().__init__(code)

    def __repr__(self) -> str:
        return f"AmoCrmBusinessWriteConfigError({self.args[0]!r})"


def _require_positive_int(raw: object, *, code: str) -> int:
    if type(raw) is not str or not raw:
        raise AmoCrmBusinessWriteConfigError(code) from None
    if any(ch.isspace() for ch in raw) or not raw.isdigit():
        raise AmoCrmBusinessWriteConfigError(code) from None
    value = int(raw)
    if value <= 0:
        raise AmoCrmBusinessWriteConfigError(code) from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class AmoCrmBusinessWriteConfig:
    """Runtime gate for business contact/deal/note/task writes."""

    enabled: bool = False
    pipeline_id: int | None = None
    open_status_id: int | None = None
    manager_id: int | None = None
    task_type_id: int | None = None
    rest: AmoCrmCrmRestConfig | None = None

    def __repr__(self) -> str:
        return (
            "AmoCrmBusinessWriteConfig("
            f"enabled={self.enabled!r}, "
            f"pipeline_id={self.pipeline_id!r}, "
            f"open_status_id={self.open_status_id!r}, "
            f"manager_id={self.manager_id!r}, "
            f"task_type_id={self.task_type_id!r}, "
            "rest=<redacted>)"
        )

    def require_runtime(self) -> None:
        if not self.enabled:
            raise AmoCrmBusinessWriteConfigError(
                "AMOCRM_CRM_BUSINESS_WRITE_DISABLED"
            ) from None
        if (
            self.pipeline_id is None
            or self.open_status_id is None
            or self.manager_id is None
            or self.task_type_id is None
            or self.rest is None
        ):
            raise AmoCrmBusinessWriteConfigError(
                "AMOCRM_CRM_BUSINESS_WRITE_CONFIG_INVALID"
            ) from None
        self.rest.require_runtime()

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AmoCrmBusinessWriteConfig:
        source = os.environ if environ is None else environ
        enabled_raw = source.get("AMOCRM_CRM_BUSINESS_WRITE_ENABLED", "false")
        if enabled_raw == "false":
            return cls(enabled=False)
        if enabled_raw != "true":
            raise AmoCrmBusinessWriteConfigError(
                "AMOCRM_CRM_BUSINESS_WRITE_CONFIG_INVALID"
            ) from None

        try:
            rest = AmoCrmCrmRestConfig.from_env(source)
        except AmoCrmCrmRestConfigError as exc:
            raise AmoCrmBusinessWriteConfigError(str(exc.args[0])) from None
        if not rest.enabled:
            raise AmoCrmBusinessWriteConfigError("AMOCRM_CRM_REST_DISABLED") from None

        pipeline_id = _require_positive_int(
            source.get("AMOCRM_CRM_BUSINESS_PIPELINE_ID"),
            code="AMOCRM_CRM_BUSINESS_PIPELINE_ID_INVALID",
        )
        open_status_id = _require_positive_int(
            source.get("AMOCRM_CRM_BUSINESS_STATUS_ID"),
            code="AMOCRM_CRM_BUSINESS_STATUS_ID_INVALID",
        )
        manager_id = _require_positive_int(
            source.get("AMOCRM_CRM_BUSINESS_MANAGER_ID"),
            code="AMOCRM_CRM_BUSINESS_MANAGER_ID_INVALID",
        )
        task_type_id = _require_positive_int(
            source.get("AMOCRM_CRM_BUSINESS_TASK_TYPE_ID"),
            code="AMOCRM_CRM_BUSINESS_TASK_TYPE_ID_INVALID",
        )
        return cls(
            enabled=True,
            pipeline_id=pipeline_id,
            open_status_id=open_status_id,
            manager_id=manager_id,
            task_type_id=task_type_id,
            rest=rest,
        )


def load_business_write_config_fail_closed(
    environ: Mapping[str, str] | None = None,
) -> AmoCrmBusinessWriteConfig:
    try:
        return AmoCrmBusinessWriteConfig.from_env(environ)
    except AmoCrmBusinessWriteConfigError:
        return AmoCrmBusinessWriteConfig(enabled=False)
    except AmoCrmCrmRestConfigError:
        return AmoCrmBusinessWriteConfig(enabled=False)
