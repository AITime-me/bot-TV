"""Unit tests for AmoCrmBusinessWriteConfig (no live CRM)."""

from __future__ import annotations

from app.core.amocrm_crm_business_write_config import (
    AmoCrmBusinessWriteConfig,
    load_business_write_config_fail_closed,
)


def test_business_write_default_disabled() -> None:
    cfg = load_business_write_config_fail_closed({})
    assert cfg.enabled is False
    assert cfg.pipeline_id is None


def test_business_write_enabled_requires_ids() -> None:
    cfg = load_business_write_config_fail_closed(
        {
            "AMOCRM_CRM_BUSINESS_WRITE_ENABLED": "true",
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": "crm-client-id-001",
            "AMOCRM_CLIENT_SECRET": "crm-secret-xxxxxxxxxx",
            "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
            "AMOCRM_CRM_REDIRECT_URI": "https://example.com/oauth",
            "AMOCRM_CRM_BUSINESS_PIPELINE_ID": "1001",
            "AMOCRM_CRM_BUSINESS_STATUS_ID": "2002",
            "AMOCRM_CRM_BUSINESS_MANAGER_ID": "3003",
            "AMOCRM_CRM_BUSINESS_TASK_TYPE_ID": "4004",
        }
    )
    assert cfg.enabled is True
    assert cfg.pipeline_id == 1001
    assert cfg.open_status_id == 2002
    assert cfg.manager_id == 3003
    assert cfg.task_type_id == 4004


def test_business_write_invalid_fail_closed() -> None:
    cfg = load_business_write_config_fail_closed(
        {
            "AMOCRM_CRM_BUSINESS_WRITE_ENABLED": "true",
            "AMOCRM_CRM_REST_ENABLED": "true",
            "AMOCRM_CLIENT_ID": "crm-client-id-001",
            "AMOCRM_CLIENT_SECRET": "crm-secret-xxxxxxxxxx",
            "AMOCRM_CRM_API_BASE_URL": "https://example.amocrm.ru",
            "AMOCRM_CRM_REDIRECT_URI": "https://example.com/oauth",
            # missing pipeline ids → fail closed disabled
        }
    )
    assert cfg.enabled is False


def test_no_hardcoded_production_ids_in_config_module() -> None:
    from pathlib import Path

    text = Path("app/core/amocrm_crm_business_write_config.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("6702678", "87911490", "9655458", "3176798"):
        assert forbidden not in text
    text2 = Path("app/core/amocrm_crm_writes_http.py").read_text(encoding="utf-8")
    for forbidden in ("6702678", "87911490", "9655458", "3176798"):
        assert forbidden not in text2
    assert AmoCrmBusinessWriteConfig(enabled=False).enabled is False
