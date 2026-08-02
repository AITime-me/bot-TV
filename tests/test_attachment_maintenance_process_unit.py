"""Unit/process tests for attachment maintenance entrypoint (Stage 2A).

No PostgreSQL, Docker, channels, or WorkerRuntime. Deterministic fakes only.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.core.attachment_keys import EnvAttachmentKeyProvider
from app.core.attachment_maintenance_types import AttachmentMaintenanceConfig
from app.core.attachment_types import AttachmentError, AttachmentSpoolPolicy
import app.attachment_maintenance as maintenance_mod
from app.attachment_maintenance import (
    _parse_spool_ttl_seconds,
    _require_existing_spool_root,
    main,
    run_attachment_maintenance,
)

_FAKE_DB = "postgresql+asyncpg://bot:secret@127.0.0.1:5432/bot_tv_test"
_KEY_B64 = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
_KEY_ID = "ATTK1"


def _key_env(**extra: str) -> dict[str, str]:
    env = {
        "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": _KEY_ID,
        f"ATTACHMENT_SPOOL_KEY_{_KEY_ID}": _KEY_B64,
    }
    env.update(extra)
    return env


def _enabled_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "attachment_maintenance_enabled": True,
        "database_url": _FAKE_DB,
        "attachment_maintenance_interval_seconds": 60,
        "attachment_maintenance_initial_delay_seconds": 0,
        "attachment_reconcile_batch_limit": 100,
        "attachment_purge_batch_limit": 100,
    }
    values.update(overrides)
    return Settings(**values)


class _FakeEngine:
    def __init__(self, *, dispose_error: Exception | None = None) -> None:
        self.dispose_calls = 0
        self.dispose_error = dispose_error

    async def dispose(self) -> None:
        self.dispose_calls += 1
        if self.dispose_error is not None:
            raise self.dispose_error


class _RecordingRunner:
    instances: list[_RecordingRunner] = []

    def __init__(self, *, store: object, config: AttachmentMaintenanceConfig) -> None:
        self.store = store
        self.config = config
        self.run_forever_calls: list[asyncio.Event] = []
        self.run_result: BaseException | None = None
        _RecordingRunner.instances.append(self)

    async def run_forever(self, *, stop_event: asyncio.Event) -> None:
        self.run_forever_calls.append(stop_event)
        if self.run_result is not None:
            raise self.run_result


class _ThrowingLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("throwing-attachment-maintenance")

    def log(self, level: int, msg: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("logger_boom")


def _install_enabled_fakes(
    monkeypatch: pytest.MonkeyPatch,
    engine: _FakeEngine,
    *,
    runner: type[_RecordingRunner] = _RecordingRunner,
) -> None:
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())
    monkeypatch.setattr(maintenance_mod, "AttachmentMaintenanceRunner", runner)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _count_event(messages: list[str], event: str) -> int:
    return sum(1 for message in messages if message.startswith(event))


def _assert_no_secrets(
    blob: str,
    *,
    root: Path | None = None,
    extra: tuple[str, ...] = (),
) -> None:
    assert _FAKE_DB not in blob
    assert "secret" not in blob
    assert _KEY_B64 not in blob
    assert _KEY_ID not in blob
    if root is not None:
        assert str(root) not in blob
    for item in extra:
        assert item not in blob


def _runner_raises(result: BaseException):
    def init_fail(
        self: _RecordingRunner,
        *,
        store: object,
        config: AttachmentMaintenanceConfig,
    ) -> None:
        _RecordingRunner.instances.append(self)
        self.store = store
        self.config = config
        self.run_forever_calls = []
        self.run_result = result

    return init_fail


@pytest.fixture(autouse=True)
def _reset_recording_runner() -> None:
    _RecordingRunner.instances.clear()


def test_settings_maintenance_defaults_are_fail_closed() -> None:
    settings = Settings.from_env({})
    assert settings.attachment_maintenance_enabled is False
    assert settings.attachment_maintenance_interval_seconds == 60
    assert settings.attachment_maintenance_initial_delay_seconds == 0
    assert settings.attachment_reconcile_batch_limit == 100
    assert settings.attachment_purge_batch_limit == 100
    assert settings.database_url is None
    settings.validate_attachment_maintenance_runtime()


def test_settings_maintenance_exact_bool() -> None:
    assert (
        Settings.from_env(
            {"ATTACHMENT_MAINTENANCE_ENABLED": "true"}
        ).attachment_maintenance_enabled
        is True
    )
    assert (
        Settings.from_env(
            {"ATTACHMENT_MAINTENANCE_ENABLED": "false"}
        ).attachment_maintenance_enabled
        is False
    )
    with pytest.raises(ValueError, match="ATTACHMENT_MAINTENANCE_ENABLED"):
        Settings.from_env({"ATTACHMENT_MAINTENANCE_ENABLED": "True"})
    with pytest.raises(ValueError, match="ATTACHMENT_MAINTENANCE_ENABLED"):
        Settings.from_env({"ATTACHMENT_MAINTENANCE_ENABLED": "1"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ATTACHMENT_MAINTENANCE_INTERVAL_SECONDS", "0"),
        ("ATTACHMENT_MAINTENANCE_INTERVAL_SECONDS", "86401"),
        ("ATTACHMENT_MAINTENANCE_INTERVAL_SECONDS", "nope"),
        ("ATTACHMENT_MAINTENANCE_INITIAL_DELAY_SECONDS", "-1"),
        ("ATTACHMENT_MAINTENANCE_INITIAL_DELAY_SECONDS", "86401"),
        ("ATTACHMENT_RECONCILE_BATCH_LIMIT", "0"),
        ("ATTACHMENT_RECONCILE_BATCH_LIMIT", "1001"),
        ("ATTACHMENT_PURGE_BATCH_LIMIT", "0"),
        ("ATTACHMENT_PURGE_BATCH_LIMIT", "1001"),
    ],
)
def test_settings_maintenance_invalid_ranges(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        Settings.from_env({name: value})


def test_validate_attachment_maintenance_requires_database_when_enabled() -> None:
    settings = Settings(attachment_maintenance_enabled=True, database_url=None)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        settings.validate_attachment_maintenance_runtime()


def test_spool_root_validation(tmp_path: Path) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    assert _require_existing_spool_root(
        {"ATTACHMENT_SPOOL_ROOT": str(root)}
    ) == root

    with pytest.raises(ValueError, match="ATTACHMENT_SPOOL_ROOT is required"):
        _require_existing_spool_root({})
    with pytest.raises(ValueError, match="ATTACHMENT_SPOOL_ROOT is required"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": ""})
    with pytest.raises(ValueError, match="absolute"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": "relative/spool"})
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="must exist"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": str(missing)})
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="directory"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": str(file_path)})


def test_spool_root_symlink_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="must not be a symlink"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": str(link)})


def test_spool_root_dangling_symlink_rejected(tmp_path: Path) -> None:
    link = tmp_path / "dangling"
    try:
        link.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="must not be a symlink"):
        _require_existing_spool_root({"ATTACHMENT_SPOOL_ROOT": str(link)})


def test_spool_ttl_default_and_invalid() -> None:
    assert _parse_spool_ttl_seconds({}) == 900
    assert _parse_spool_ttl_seconds({"ATTACHMENT_SPOOL_TTL_SECONDS": "120"}) == 120
    with pytest.raises(ValueError, match="ATTACHMENT_SPOOL_TTL_SECONDS"):
        _parse_spool_ttl_seconds({"ATTACHMENT_SPOOL_TTL_SECONDS": "0"})
    with pytest.raises(ValueError, match="ATTACHMENT_SPOOL_TTL_SECONDS"):
        _parse_spool_ttl_seconds({"ATTACHMENT_SPOOL_TTL_SECONDS": "86401"})
    with pytest.raises(ValueError, match="ATTACHMENT_SPOOL_TTL_SECONDS"):
        _parse_spool_ttl_seconds({"ATTACHMENT_SPOOL_TTL_SECONDS": "true"})


@pytest.mark.asyncio
async def test_disabled_exits_without_creating_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []

    def boom(*_args: object, **_kwargs: object) -> None:
        calls.append("create_engine")
        raise AssertionError("create_engine must not be called when disabled")

    monkeypatch.setattr(maintenance_mod, "create_engine", boom)
    monkeypatch.setattr(
        maintenance_mod,
        "EnvAttachmentKeyProvider",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("key provider must not be created")
        ),
    )
    monkeypatch.setattr(
        maintenance_mod,
        "AttachmentSpoolStore",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("store")),
    )
    monkeypatch.setattr(
        maintenance_mod,
        "AttachmentMaintenanceRunner",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("runner")),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        await run_attachment_maintenance(Settings.from_env({}), environ={})

    assert calls == []
    events = [record.getMessage() for record in caplog.records]
    assert "attachment_maintenance_process_starting" in events
    assert "attachment_maintenance_process_disabled" in events
    assert "attachment_maintenance_process_started" not in events


@pytest.mark.asyncio
async def test_enabled_construction_order_and_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    order: list[str] = []
    engine = _FakeEngine()
    captured: dict[str, Any] = {}

    def fake_create_engine(settings: Settings) -> _FakeEngine:
        order.append("engine")
        captured["settings"] = settings
        return engine

    def fake_session_factory(eng: object) -> object:
        order.append("session_factory")
        assert eng is engine
        return object()

    class TrackingKeyProvider:
        def __init__(self, environ: Mapping[str, str] | None = None) -> None:
            order.append("key_provider")
            self._inner = EnvAttachmentKeyProvider(environ)

        def get_active_key(self) -> object:
            order.append("key_probe")
            return self._inner.get_active_key()

        def active_key_id(self) -> str:
            return self._inner.active_key_id()

        def get_key(self, key_id: str) -> bytes:
            return self._inner.get_key(key_id)

    def fake_policy(spool_root: Path, ttl_seconds: int) -> AttachmentSpoolPolicy:
        order.append("policy")
        captured["policy_root"] = spool_root
        captured["policy_ttl"] = ttl_seconds
        return AttachmentSpoolPolicy(spool_root, ttl_seconds)

    def fake_store(**kwargs: object) -> object:
        order.append("store")
        captured["store_kwargs"] = kwargs
        return object()

    def fake_runner(*, store: object, config: AttachmentMaintenanceConfig) -> _RecordingRunner:
        order.append("runner")
        return _RecordingRunner(store=store, config=config)

    monkeypatch.setattr(maintenance_mod, "create_engine", fake_create_engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", fake_session_factory)
    monkeypatch.setattr(maintenance_mod, "EnvAttachmentKeyProvider", TrackingKeyProvider)
    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolPolicy", fake_policy)
    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolStore", fake_store)
    monkeypatch.setattr(maintenance_mod, "AttachmentMaintenanceRunner", fake_runner)

    settings = _enabled_settings(
        attachment_maintenance_interval_seconds=42,
        attachment_maintenance_initial_delay_seconds=3,
        attachment_reconcile_batch_limit=7,
        attachment_purge_batch_limit=9,
    )
    env = _key_env(
        ATTACHMENT_SPOOL_ROOT=str(root),
        ATTACHMENT_SPOOL_TTL_SECONDS="450",
    )
    await run_attachment_maintenance(settings, environ=env)

    assert order == [
        "key_provider",
        "key_probe",
        "engine",
        "session_factory",
        "policy",
        "store",
        "runner",
    ]
    assert engine.dispose_calls == 1
    runner = _RecordingRunner.instances[0]
    assert len(runner.run_forever_calls) == 1
    assert isinstance(runner.run_forever_calls[0], asyncio.Event)
    assert runner.config.interval_seconds == 42
    assert runner.config.initial_delay_seconds == 3
    assert runner.config.reconcile_limit == 7
    assert runner.config.purge_limit == 9
    assert captured["policy_root"] == root
    assert captured["policy_ttl"] == 450
    assert isinstance(captured["store_kwargs"]["policy"], AttachmentSpoolPolicy)


@pytest.mark.asyncio
async def test_missing_database_url_when_enabled_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    monkeypatch.setattr(
        maintenance_mod,
        "create_engine",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("engine")),
    )
    settings = Settings(attachment_maintenance_enabled=True, database_url=None)
    with caplog.at_level(logging.ERROR, logger=maintenance_mod.__name__):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            await run_attachment_maintenance(
                settings,
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    assert any(
        "attachment_maintenance_process_startup_failed" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_missing_key_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    created: list[str] = []

    def track_engine(*_a: object, **_k: object) -> None:
        created.append("engine")
        raise AssertionError("engine must not be created before key probe")

    monkeypatch.setattr(maintenance_mod, "create_engine", track_engine)
    with caplog.at_level(logging.ERROR, logger=maintenance_mod.__name__):
        with pytest.raises(AttachmentError) as raised:
            await run_attachment_maintenance(
                _enabled_settings(),
                environ={"ATTACHMENT_SPOOL_ROOT": str(root)},
            )
    assert raised.value.code == "ATTACHMENT_KEY_UNAVAILABLE"
    assert created == []
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "attachment_maintenance_process_startup_failed" in blob
    assert "error_code=ATTACHMENT_KEY_UNAVAILABLE" in blob
    assert _KEY_B64 not in blob
    assert _KEY_ID not in blob
    assert f"ATTACHMENT_SPOOL_KEY_{_KEY_ID}" not in blob
    assert str(root) not in blob
    assert _FAKE_DB not in blob
    assert "secret" not in blob


@pytest.mark.asyncio
async def test_invalid_key_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    monkeypatch.setattr(
        maintenance_mod,
        "create_engine",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("engine")),
    )
    with pytest.raises(AttachmentError) as raised:
        await run_attachment_maintenance(
            _enabled_settings(),
            environ={
                "ATTACHMENT_SPOOL_ROOT": str(root),
                "ATTACHMENT_SPOOL_ACTIVE_KEY_ID": "ATTK1",
                "ATTACHMENT_SPOOL_KEY_ATTK1": "not-valid-key-material!!!",
            },
        )
    assert raised.value.code == "ATTACHMENT_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_dispose_after_runner_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(RuntimeError("boom_runtime_fatal_sentinel")),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(RuntimeError, match="boom_runtime_fatal_sentinel"):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert any(
        msg.startswith("attachment_maintenance_process_fatal")
        and "error_code=RuntimeError" in msg
        for msg in messages
    )
    _assert_no_secrets(
        " ".join(messages),
        root=root,
        extra=("boom_runtime_fatal_sentinel",),
    )


@pytest.mark.asyncio
async def test_dispose_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(asyncio.CancelledError()),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(asyncio.CancelledError):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0


@pytest.mark.asyncio
async def test_dispose_failure_does_not_mask_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine(dispose_error=RuntimeError("dispose_secret_sentinel"))
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(asyncio.CancelledError()),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(asyncio.CancelledError):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    _assert_no_secrets(
        " ".join(messages),
        root=root,
        extra=("dispose_secret_sentinel",),
    )


@pytest.mark.asyncio
async def test_primary_fatal_plus_dispose_failure_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    primary = "primary_fatal_valueerror_sentinel"
    secondary = "dispose_runtime_secret_sentinel"
    engine = _FakeEngine(dispose_error=RuntimeError(secondary))
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(ValueError(primary)),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(ValueError, match=primary):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert any(
        msg.startswith("attachment_maintenance_process_fatal")
        and "error_code=ValueError" in msg
        for msg in messages
    )
    assert not any("error_code=RuntimeError" in msg for msg in messages)
    _assert_no_secrets(" ".join(messages), root=root, extra=(primary, secondary))


@pytest.mark.asyncio
async def test_dispose_failure_after_clean_run_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    dispose_msg = "dispose_clean_run_secret_sentinel"
    engine = _FakeEngine(dispose_error=RuntimeError(dispose_msg))
    _install_enabled_fakes(monkeypatch, engine)

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(RuntimeError, match=dispose_msg):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_stopping") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert any(
        msg.startswith("attachment_maintenance_process_fatal")
        and "error_code=RuntimeError" in msg
        for msg in messages
    )
    _assert_no_secrets(" ".join(messages), root=root, extra=(dispose_msg,))


@pytest.mark.asyncio
async def test_dispose_after_session_factory_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)

    def fail_session_factory(_engine: object) -> object:
        raise RuntimeError("session_factory_secret")

    monkeypatch.setattr(
        maintenance_mod,
        "create_session_factory",
        fail_session_factory,
    )
    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(RuntimeError, match="session_factory_secret"):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_startup_failed") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert any(
        "attachment_maintenance_process_startup_failed" in msg
        and "error_code=RuntimeError" in msg
        for msg in messages
    )


@pytest.mark.asyncio
async def test_dispose_after_policy_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())

    def fail_policy(*_a: object, **_k: object) -> AttachmentSpoolPolicy:
        raise RuntimeError("policy_secret")

    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolPolicy", fail_policy)
    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(RuntimeError, match="policy_secret"):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_startup_failed") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0


@pytest.mark.asyncio
async def test_dispose_after_runner_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())

    def fail_runner(*, store: object, config: AttachmentMaintenanceConfig) -> object:
        raise RuntimeError("runner_ctor_secret")

    monkeypatch.setattr(maintenance_mod, "AttachmentMaintenanceRunner", fail_runner)
    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(RuntimeError, match="runner_ctor_secret"):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_startup_failed") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0


@pytest.mark.asyncio
async def test_no_dispose_when_engine_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    dispose_calls = {"n": 0}

    class ExplodingEngine:
        async def dispose(self) -> None:
            dispose_calls["n"] += 1

    def fail_create(_settings: Settings) -> ExplodingEngine:
        raise RuntimeError("engine_create_failed")

    monkeypatch.setattr(maintenance_mod, "create_engine", fail_create)
    with pytest.raises(RuntimeError, match="engine_create_failed"):
        await run_attachment_maintenance(
            _enabled_settings(),
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )
    assert dispose_calls["n"] == 0


@pytest.mark.asyncio
async def test_signal_registration_and_stop_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    registered: list[tuple[signal.Signals, object]] = []

    class FakeLoop:
        def add_signal_handler(self, signum: signal.Signals, callback: object) -> None:
            registered.append((signum, callback))

    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())

    await run_attachment_maintenance(
        _enabled_settings(),
        environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
    )

    assert {signum for signum, _ in registered} == {signal.SIGINT, signal.SIGTERM}
    stop_event = _RecordingRunner.instances[0].run_forever_calls[0]
    assert stop_event.is_set() is False
    callbacks = [callback for _signum, callback in registered]
    assert callbacks == [stop_event.set, stop_event.set]
    # Invoke the registered callbacks (signal path), not stop_event.set directly.
    callbacks[0]()
    assert stop_event.is_set() is True
    callbacks[1]()
    assert stop_event.is_set() is True


@pytest.mark.asyncio
async def test_unsupported_signal_handler_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()

    class UnsupportedLoop:
        def add_signal_handler(self, *_a: object, **_k: object) -> None:
            raise NotImplementedError("windows")

    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())
    monkeypatch.setattr(
        maintenance_mod,
        "AttachmentMaintenanceRunner",
        _RecordingRunner,
    )
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: UnsupportedLoop())

    await run_attachment_maintenance(
        _enabled_settings(),
        environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
    )
    assert engine.dispose_calls == 1
    assert len(_RecordingRunner.instances[0].run_forever_calls) == 1


@pytest.mark.asyncio
async def test_throwing_logger_does_not_mask_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(asyncio.CancelledError()),
    )
    monkeypatch.setattr(maintenance_mod, "logger", _ThrowingLogger())

    with pytest.raises(asyncio.CancelledError):
        await run_attachment_maintenance(
            _enabled_settings(),
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_throwing_logger_preserves_fatal_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(RuntimeError("fatal_secret_text")),
    )
    monkeypatch.setattr(maintenance_mod, "logger", _ThrowingLogger())

    with pytest.raises(RuntimeError, match="fatal_secret_text"):
        await run_attachment_maintenance(
            _enabled_settings(),
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )
    assert engine.dispose_calls == 1


def test_main_keyboard_interrupt_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: Settings()),
    )

    def raise_ki(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance_mod.asyncio, "run", raise_ki)
    assert main() == 0


def test_main_unexpected_exception_returns_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: Settings()),
    )

    def raise_exc(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()
        raise RuntimeError("secret-db-password-should-not-leak")

    monkeypatch.setattr(maintenance_mod.asyncio, "run", raise_exc)
    assert main() == 1
    err = capsys.readouterr().err
    assert "attachment_maintenance stopped error_code=RuntimeError" in err
    assert "secret-db-password-should-not-leak" not in err


def test_main_disabled_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Settings] = []

    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: Settings()),
    )

    async def fake_run(
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        observed.append(settings)
        assert settings.attachment_maintenance_enabled is False

    monkeypatch.setattr(maintenance_mod, "run_attachment_maintenance", fake_run)
    assert main() == 0
    assert len(observed) == 1


def test_module_import_has_no_side_effects() -> None:
    assert callable(run_attachment_maintenance)
    assert callable(main)
    assert maintenance_mod.create_engine.__module__ == "app.db.session"


def test_isolation_no_worker_runtime_or_main_imports() -> None:
    source = Path(maintenance_mod.__file__).read_text(encoding="utf-8")
    assert "from app.main" not in source
    assert "import app.main" not in source
    assert "from app.services.worker_runtime" not in source
    assert "import app.services.worker_runtime" not in source
    assert "from app.channels" not in source
    assert "amocrm" not in source.lower()
    assert "telegram" not in source.lower()


@pytest.mark.asyncio
async def test_safe_logging_events_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine()
    _install_enabled_fakes(monkeypatch, engine)
    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        await run_attachment_maintenance(
            _enabled_settings(),
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )
    messages = _messages(caplog)
    assert _count_event(messages, "attachment_maintenance_process_starting") == 1
    assert _count_event(messages, "attachment_maintenance_process_started") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopping") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    stopped_idx = next(
        i
        for i, msg in enumerate(messages)
        if msg.startswith("attachment_maintenance_process_stopped")
    )
    stopping_idx = next(
        i
        for i, msg in enumerate(messages)
        if msg.startswith("attachment_maintenance_process_stopping")
    )
    assert stopping_idx < stopped_idx
    assert engine.dispose_calls == 1
    _assert_no_secrets(" ".join(messages), root=root)


@pytest.mark.asyncio
async def test_default_ttl_applied_in_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    captured: dict[str, int] = {}

    def capture_policy(spool_root: Path, ttl_seconds: int) -> AttachmentSpoolPolicy:
        captured["ttl"] = ttl_seconds
        return AttachmentSpoolPolicy(spool_root, ttl_seconds)

    engine = _FakeEngine()
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())
    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolPolicy", capture_policy)
    monkeypatch.setattr(
        maintenance_mod,
        "AttachmentMaintenanceRunner",
        _RecordingRunner,
    )
    await run_attachment_maintenance(
        _enabled_settings(),
        environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
    )
    assert captured["ttl"] == 900


@pytest.mark.asyncio
async def test_store_constructor_failure_disposes_and_startup_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    store_msg = "store_ctor_unique_valueerror_sentinel"
    token_sentinel = "lease-token-sentinel-n5-xyz"
    engine = _FakeEngine()
    runner_created = {"n": 0}

    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())

    def fail_store(**_kwargs: object) -> object:
        raise ValueError(store_msg)

    def tracking_runner(*, store: object, config: AttachmentMaintenanceConfig) -> object:
        runner_created["n"] += 1
        raise AssertionError("runner must not be constructed")

    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolStore", fail_store)
    monkeypatch.setattr(maintenance_mod, "AttachmentMaintenanceRunner", tracking_runner)

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(ValueError, match=store_msg):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert runner_created["n"] == 0
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_startup_failed") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert any(
        msg.startswith("attachment_maintenance_process_startup_failed")
        and "error_code=ValueError" in msg
        for msg in messages
    )
    _assert_no_secrets(
        " ".join(messages),
        root=root,
        extra=(store_msg, token_sentinel),
    )


def test_store_constructor_failure_main_boundary_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store_msg = "store_ctor_main_valueerror_sentinel"
    token_sentinel = "lease-token-sentinel-n5-main"

    async def boom_run(
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        raise ValueError(store_msg)

    monkeypatch.setattr(maintenance_mod, "run_attachment_maintenance", boom_run)
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: Settings()),
    )
    assert main() == 1
    err = capsys.readouterr().err
    assert err.strip() == "attachment_maintenance stopped error_code=ValueError"
    assert store_msg not in err
    assert _FAKE_DB not in err
    assert _KEY_B64 not in err
    assert _KEY_ID not in err
    assert token_sentinel not in err


def test_primary_fatal_plus_dispose_failure_main_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    primary = "main_boundary_primary_valueerror_sentinel"
    secondary = "main_boundary_dispose_runtime_sentinel"
    engine = _FakeEngine(dispose_error=RuntimeError(secondary))
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(ValueError(primary)),
    )

    async def invoke(
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        await run_attachment_maintenance(
            settings,
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )

    monkeypatch.setattr(maintenance_mod, "run_attachment_maintenance", invoke)
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: _enabled_settings()),
    )
    assert main() == 1
    err = capsys.readouterr().err
    assert err.strip() == "attachment_maintenance stopped error_code=ValueError"
    assert primary not in err
    assert secondary not in err
    assert "RuntimeError" not in err
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_cancel_plus_dispose_failure_plus_throwing_logger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    engine = _FakeEngine(dispose_error=RuntimeError("dispose_cancel_secret_sentinel"))
    _install_enabled_fakes(monkeypatch, engine)
    monkeypatch.setattr(
        _RecordingRunner,
        "__init__",
        _runner_raises(asyncio.CancelledError()),
    )
    monkeypatch.setattr(maintenance_mod, "logger", _ThrowingLogger())

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(asyncio.CancelledError):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0


def test_clean_run_dispose_failure_main_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    dispose_msg = "clean_dispose_main_secret_sentinel"
    engine = _FakeEngine(dispose_error=RuntimeError(dispose_msg))
    _install_enabled_fakes(monkeypatch, engine)

    async def invoke(
        settings: Settings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        await run_attachment_maintenance(
            _enabled_settings(),
            environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
        )

    monkeypatch.setattr(maintenance_mod, "run_attachment_maintenance", invoke)
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, environ=None: Settings()),
    )
    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        assert main() == 1
    err = capsys.readouterr().err
    assert err.strip() == "attachment_maintenance stopped error_code=RuntimeError"
    assert dispose_msg not in err
    messages = _messages(caplog)
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 1
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_startup_primary_plus_dispose_failure_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "spool"
    root.mkdir()
    primary = "startup_store_valueerror_sentinel"
    secondary = "startup_dispose_runtime_sentinel"
    engine = _FakeEngine(dispose_error=RuntimeError(secondary))
    monkeypatch.setattr(maintenance_mod, "create_engine", lambda _s: engine)
    monkeypatch.setattr(maintenance_mod, "create_session_factory", lambda _e: object())

    def fail_store(**_kwargs: object) -> object:
        raise ValueError(primary)

    monkeypatch.setattr(maintenance_mod, "AttachmentSpoolStore", fail_store)
    monkeypatch.setattr(
        maintenance_mod,
        "AttachmentMaintenanceRunner",
        lambda **_k: (_ for _ in ()).throw(AssertionError("runner")),
    )

    with caplog.at_level(logging.INFO, logger=maintenance_mod.__name__):
        with pytest.raises(ValueError, match=primary):
            await run_attachment_maintenance(
                _enabled_settings(),
                environ=_key_env(ATTACHMENT_SPOOL_ROOT=str(root)),
            )
    messages = _messages(caplog)
    assert engine.dispose_calls == 1
    assert _count_event(messages, "attachment_maintenance_process_startup_failed") == 1
    assert _count_event(messages, "attachment_maintenance_process_fatal") == 0
    assert _count_event(messages, "attachment_maintenance_process_stopped") == 0
    _assert_no_secrets(" ".join(messages), root=root, extra=(primary, secondary))
