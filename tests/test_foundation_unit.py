from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import Settings, redact_database_url
from app.db import session as session_module
from app.db.base import Base
from app.db.session import _DEFAULT_ISOLATION_LEVEL, create_engine, session_scope
from app.models.conversation import (
    Conversation,
    ConversationStatus,
    HandoffState,
    conversation_allows_automatic_reply,
)
from app.models.outbox import DeliveryStatus, DestinationType
from app.schemas.inbound import SyntheticInboundEvent
from app.services.inbound import assert_no_client_outbound_path
from tests.foundation_test_db import (
    AlembicCommandError,
    PgDatabaseUnavailableError,
    SecretDatabaseUrl,
    UnsafeTestDatabaseError,
    assert_safe_test_database_url,
    describe_database_target,
    resolve_secret_test_database_url,
    resolve_test_database_url,
    run_alembic_command,
    run_alembic_command_async,
    scrub_secrets,
)

import app.models  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Synthetic credentials used only to prove redaction; never a real secret.
_FAKE_PASSWORD = "unit-test-fake-password"
_FAKE_URL = (
    f"postgresql+asyncpg://bot:{_FAKE_PASSWORD}@127.0.0.1:5432/bot_tv_foundation_test"
)


def test_database_url_optional_and_validated() -> None:
    assert Settings.from_env({}).database_url is None
    assert Settings.from_env({"DATABASE_URL": ""}).database_url is None
    settings = Settings.from_env(
        {"DATABASE_URL": "postgresql+asyncpg://bot:x@127.0.0.1:5432/bot"}
    )
    assert settings.database_url is not None
    assert settings.async_database_url.startswith("postgresql+asyncpg://")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings.from_env({"DATABASE_URL": "mysql://bad"})


def test_resolve_test_url_ignores_database_url() -> None:
    environ = {
        "DATABASE_URL": "postgresql+asyncpg://bot:secret@127.0.0.1:5432/production",
    }
    assert resolve_test_database_url(environ) is None


def test_resolve_test_url_reads_only_bot_tv_test_database_url() -> None:
    environ = {
        "DATABASE_URL": "postgresql+asyncpg://bot:secret@127.0.0.1:5432/production",
        "BOT_TV_TEST_DATABASE_URL": (
            "postgresql+asyncpg://bot:secret@127.0.0.1:5432/bot_tv_test"
        ),
    }
    assert resolve_test_database_url(environ) == environ["BOT_TV_TEST_DATABASE_URL"]


@pytest.mark.parametrize(
    "database_name",
    [
        "bot_tv_foundation_test",
        "test_bot_tv",
        "bot-tv-test",
        "bot_tv_test",
    ],
)
def test_assert_safe_test_database_url_allows_discrete_test_segment(
    database_name: str,
) -> None:
    url = f"postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/{database_name}"
    assert assert_safe_test_database_url(url) == database_name


def test_assert_safe_test_database_url_allows_query_params() -> None:
    name = assert_safe_test_database_url(
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/"
        "bot_tv_foundation_test?sslmode=disable"
    )
    assert name == "bot_tv_foundation_test"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/latest_bot",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/contest",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/protest",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/bot_tv",
        # marker only in query / username / password — not in DB name
        "postgresql+asyncpg://test:super-secret@127.0.0.1:5432/bot_tv",
        "postgresql+asyncpg://bot:test-secret@127.0.0.1:5432/bot_tv",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/bot_tv"
        "?options=-csearch_path%3Dtest",
        "mysql://bot:super-secret@127.0.0.1:5432/bot_tv_test",
        "not-a-url",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:5432/",
    ],
)
def test_assert_safe_test_database_url_rejects_unsafe_names(url: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        assert_safe_test_database_url(url)
    message = str(exc.value)
    assert "super-secret" not in message
    assert "test-secret" not in message
    assert "://" not in message


def test_pytest_ini_uses_session_loop_for_fixtures_and_tests() -> None:
    text = (_REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "asyncio_default_fixture_loop_scope = session" in text
    assert "asyncio_default_test_loop_scope = session" in text


def test_pg_harness_does_not_restore_schema_via_create_all() -> None:
    for relative in (
        ("tests", "conftest.py"),
        ("tests", "pg_harness.py"),
        ("tests", "test_foundation_pg.py"),
    ):
        source = _REPO_ROOT.joinpath(*relative).read_text(encoding="utf-8")
        assert "Base.metadata.create_all" not in source
        assert ".create_all(" not in source
    conftest = (_REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    harness = (_REPO_ROOT / "tests" / "pg_harness.py").read_text(encoding="utf-8")
    assert "run_alembic_command_async" in conftest
    assert "assert_postgres_reachable" in harness
    assert "pytest.skip" in harness
    assert "except Exception:\n        return False" not in harness
    assert "except Exception:\n        pytest.skip" not in harness
    foundation_pg = (_REPO_ROOT / "tests" / "test_foundation_pg.py").read_text(
        encoding="utf-8"
    )
    assert "run_alembic_command_async" in foundation_pg


def test_pg_harness_hides_test_database_url() -> None:
    conftest = (_REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    harness = (_REPO_ROOT / "tests" / "pg_harness.py").read_text(encoding="utf-8")
    # The session fixture must hand out the redacting wrapper, not a raw str.
    assert "def pg_database_url() -> SecretDatabaseUrl:" in conftest
    assert "resolve_secret_test_database_url" in harness
    assert "scrub_secrets" in harness
    assert "secret.target()" in harness
    # The revealed URL must be passed inline, never bound to a local variable
    # that pytest --showlocals would print.
    combined = conftest + "\n" + harness
    assert (
        re.search(
            r"^\s*\w+\s*=\s*(reveal_database_url\(|\w+\.reveal\(\))",
            combined,
            re.MULTILINE,
        )
        is None
    )
    # Async fixtures must be declared, never resolved from a running loop.
    assert "getfixturevalue(" not in combined
    foundation_pg = (_REPO_ROOT / "tests" / "test_foundation_pg.py").read_text(
        encoding="utf-8"
    )
    assert "session_factory: async_sessionmaker[AsyncSession]," in foundation_pg
    assert "getfixturevalue(" not in foundation_pg
    assert "run_alembic_command_async" in conftest
    assert "assert_postgres_reachable" in harness
    assert "pytest.skip" in harness
    assert "except Exception:\n        return False" not in harness
    assert "except Exception:\n        pytest.skip" not in harness


@pytest.mark.asyncio
async def test_alembic_helper_runs_inside_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    seen_env_during_call: list[str | None] = []

    class _FakeCommands:
        @staticmethod
        def upgrade(config, revision):  # type: ignore[no-untyped-def]
            calls.append(("upgrade", revision))
            seen_env_during_call.append(os.environ.get("DATABASE_URL"))

        @staticmethod
        def downgrade(config, revision):  # type: ignore[no-untyped-def]
            calls.append(("downgrade", revision))
            seen_env_during_call.append(os.environ.get("DATABASE_URL"))

    keep = "postgresql+asyncpg://keeper:keep-secret@127.0.0.1:5432/keep_db"
    temp = (
        "postgresql+asyncpg://tmp:super-secret@127.0.0.1:5432/"
        "bot_tv_foundation_test"
    )
    monkeypatch.setenv("DATABASE_URL", keep)
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")

    # Prove we are inside a running loop (would raise without to_thread).
    assert asyncio.get_running_loop() is not None

    await run_alembic_command_async(
        alembic_ini=ini,
        command_name="upgrade",
        revision="head",
        database_url=temp,
        command_module=_FakeCommands,
    )
    await run_alembic_command_async(
        alembic_ini=ini,
        command_name="downgrade",
        revision="base",
        database_url=temp,
        command_module=_FakeCommands,
    )

    assert calls == [("upgrade", "head"), ("downgrade", "base")]
    assert seen_env_during_call == [temp, temp]
    assert os.environ.get("DATABASE_URL") == keep
    # Sync helper restores even when the command raises.
    with pytest.raises(RuntimeError, match="boom"):

        class _Boom:
            @staticmethod
            def upgrade(config, revision):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        run_alembic_command(
            alembic_ini=ini,
            command_name="upgrade",
            revision="head",
            database_url=temp,
            command_module=_Boom,
        )
    assert os.environ.get("DATABASE_URL") == keep
    # Sanitized path: helper never embeds temp password into its own errors.
    with pytest.raises(UnsafeTestDatabaseError) as exc:
        run_alembic_command(
            alembic_ini=ini,
            command_name="upgrade",
            revision="head",
            database_url=(
                "postgresql+asyncpg://tmp:super-secret@127.0.0.1:5432/latest_bot"
            ),
            command_module=_FakeCommands,
        )
    assert "super-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_unreachable_safe_test_url_fails_not_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.pg_harness import assert_postgres_reachable

    monkeypatch.setenv(
        "BOT_TV_TEST_DATABASE_URL",
        "postgresql+asyncpg://bot:super-secret@127.0.0.1:1/bot_tv_foundation_test",
    )
    url = resolve_test_database_url(os.environ)
    assert url is not None
    assert_safe_test_database_url(url)
    with pytest.raises(PgDatabaseUnavailableError) as exc:
        await assert_postgres_reachable(url)
    message = str(exc.value)
    assert "super-secret" not in message
    assert "://" not in message
    assert "unreachable" in message


def test_settings_repr_redacts_password_but_keeps_safe_parts() -> None:
    settings = Settings.from_env({"DATABASE_URL": _FAKE_URL})
    renders = [
        repr(settings),
        str(settings),
        f"{settings}",
        f"{settings!r}",
        "%s" % (settings,),
    ]
    for rendered in renders:
        assert _FAKE_PASSWORD not in rendered
        assert _FAKE_URL not in rendered
        assert "***" in rendered
        assert "127.0.0.1:5432" in rendered
        assert "bot_tv_foundation_test" in rendered
    safe_default = repr(Settings.from_env({}))
    assert "database_url=None" in safe_default
    assert safe_default.endswith("handoff_expiry_poll_seconds=1)")
    # The raw value stays reachable for the engine, only rendering is redacted.
    assert settings.database_url == _FAKE_URL


@pytest.mark.parametrize(
    "url",
    [
        _FAKE_URL,
        f"postgresql+asyncpg://bot:{_FAKE_PASSWORD}@127.0.0.1/bot_tv_test",
        f"postgresql://bot:{_FAKE_PASSWORD}@db.internal:6432/bot_tv_test?ssl=require",
        f"postgresql+asyncpg://:{_FAKE_PASSWORD}@127.0.0.1:5432/bot_tv_test",
    ],
)
def test_redact_database_url_never_renders_password(url: str) -> None:
    rendered = redact_database_url(url)
    assert _FAKE_PASSWORD not in rendered
    assert ":***@" in rendered
    assert "bot_tv" in rendered


def test_redact_database_url_handles_passwordless_and_broken_urls() -> None:
    assert (
        redact_database_url("postgresql+asyncpg://bot@127.0.0.1:5432/bot_tv_test")
        == "postgresql+asyncpg://bot@127.0.0.1:5432/bot_tv_test"
    )
    assert redact_database_url("not-a-url") == "<redacted-database-url>"
    assert "***" not in redact_database_url("postgresql+asyncpg:///bot_tv_test")


def test_engine_never_renders_password() -> None:
    engine = create_engine(Settings.from_env({"DATABASE_URL": _FAKE_URL}))
    try:
        renders = [
            repr(engine),
            str(engine),
            repr(engine.url),
            str(engine.url),
            repr(engine.sync_engine),
        ]
        for rendered in renders:
            assert _FAKE_PASSWORD not in rendered
    finally:
        # Engine creation is lazy: no connection was opened, so a sync dispose
        # is enough and keeps this test free of a running PostgreSQL.
        engine.sync_engine.dispose()


def test_secret_database_url_hides_credentials_in_every_rendering() -> None:
    secret = SecretDatabaseUrl(_FAKE_URL)
    renders = [
        repr(secret),
        str(secret),
        f"{secret}",
        f"{secret!r}",
        f"{secret:>10}",
        "%s" % (secret,),
        "url=%r" % (secret,),
    ]
    for rendered in renders:
        assert _FAKE_PASSWORD not in rendered
        assert "://" not in rendered
    assert str(secret) == "127.0.0.1:5432/bot_tv_foundation_test"
    assert secret.reveal() == _FAKE_URL
    assert _FAKE_PASSWORD not in secret.redacted()
    assert describe_database_target(secret) == "127.0.0.1:5432/bot_tv_foundation_test"


def test_resolve_secret_test_database_url_wraps_value() -> None:
    assert resolve_secret_test_database_url({}) is None
    secret = resolve_secret_test_database_url(
        {"BOT_TV_TEST_DATABASE_URL": _FAKE_URL},
    )
    assert isinstance(secret, SecretDatabaseUrl)
    assert secret.reveal() == _FAKE_URL
    assert _FAKE_PASSWORD not in repr(secret)


def test_scrub_secrets_removes_url_password_and_links() -> None:
    driver_message = (
        f"connection to {_FAKE_URL} failed: password authentication failed "
        f"(password={_FAKE_PASSWORD}) "
        "(Background on this error at: https://sqlalche.me/e/20/e3q8)"
    )
    scrubbed = scrub_secrets(driver_message, _FAKE_URL)
    assert _FAKE_PASSWORD not in scrubbed
    assert _FAKE_URL not in scrubbed
    assert "://" not in scrubbed
    assert "password authentication failed" in scrubbed
    # Works without the URL argument too: any scheme://... token is dropped.
    assert "://" not in scrub_secrets(driver_message)


def test_alembic_failure_message_is_scrubbed(tmp_path: Path) -> None:
    ini = tmp_path / "alembic.ini"
    ini.write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")

    class _Leaky:
        @staticmethod
        def upgrade(config, revision):  # type: ignore[no-untyped-def]
            raise RuntimeError(
                "could not connect to "
                f"{os.environ['DATABASE_URL']} password={_FAKE_PASSWORD}"
            )

    with pytest.raises(AlembicCommandError) as exc:
        run_alembic_command(
            alembic_ini=ini,
            command_name="upgrade",
            revision="head",
            database_url=SecretDatabaseUrl(_FAKE_URL),
            command_module=_Leaky,
        )
    message = str(exc.value)
    assert _FAKE_PASSWORD not in message
    assert _FAKE_URL not in message
    assert "://" not in message
    assert "127.0.0.1:5432/bot_tv_foundation_test" in message
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.asyncio
async def test_async_fixture_value_cannot_be_resolved_from_running_loop() -> None:
    """Regression: pytest-asyncio resolves async fixtures via Runner.run().

    Calling request.getfixturevalue() for an async fixture from inside another
    async fixture therefore errors out, which is why the PG cleanup fixture
    declares session_factory as a parameter.
    """

    async def _probe() -> None:
        return None

    runner = asyncio.Runner()
    coro = _probe()
    try:
        with pytest.raises(
            RuntimeError,
            match="cannot be called from a running event loop",
        ):
            runner.run(coro)
    finally:
        coro.close()
        runner.close()


class TestAutouseAsyncFixtureWiring:
    """Mirror of the PG cleanup fixture: autouse async fixture + async dependency.

    Declared parameters are resolved by pytest before the fixture body runs, so
    this wiring works inside the session-scoped loop; the previous
    request-based lookup raised RuntimeError during setup instead.
    """

    @pytest_asyncio.fixture
    async def inner_resource(self) -> str:
        return "resolved"

    @pytest_asyncio.fixture(autouse=True)
    async def outer_cleanup(self, inner_resource: str) -> AsyncIterator[None]:
        assert inner_resource == "resolved"
        yield
        assert inner_resource == "resolved"

    @pytest.mark.asyncio
    async def test_declared_async_dependency_is_available(
        self,
        inner_resource: str,
    ) -> None:
        assert inner_resource == "resolved"


def test_applied_check_verification_tolerates_postgres_normalization() -> None:
    """Regression: PostgreSQL rewrites `IN (...)`, so raw SQL text never matches."""
    from tests.test_foundation_pg import _assert_check_semantics, _check_literals

    single = "CHECK (((channel)::text = 'synthetic'::text))"
    multi = (
        "CHECK (((status)::text = ANY ((ARRAY['OPEN'::character varying, "
        "'HANDOFF'::character varying, 'CLOSED'::character varying])::text[])))"
    )
    assert _check_literals(single) == {"synthetic"}
    _assert_check_semantics("ck_conversations_channel", single)
    _assert_check_semantics("ck_conversations_status", multi)

    leaky = (
        "CHECK (((delivery_status)::text = ANY ((ARRAY['PENDING'::character varying, "
        "'CANCELLED'::character varying, 'SENT'::character varying])::text[])))"
    )
    with pytest.raises(AssertionError):
        _assert_check_semantics("ck_outbox_delivery_status", leaky)
    with pytest.raises(AssertionError):
        _assert_check_semantics("ck_conversations_status", single)


def test_synthetic_payload_is_safe() -> None:
    event = SyntheticInboundEvent(
        external_conversation_id="conv-1",
        external_message_id="msg-1",
        text="hello synthetic",
    )
    payload = event.safe_payload()
    assert payload == {"schema": "synthetic.inbound.v1", "text": "hello synthetic"}


def test_synthetic_event_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SyntheticInboundEvent(
            external_conversation_id="conv-1",
            external_message_id="msg-1",
            text="hello",
            phone="+10000000000",  # type: ignore[call-arg]
        )


def test_synthetic_event_rejects_token_field() -> None:
    with pytest.raises(ValidationError):
        SyntheticInboundEvent.model_validate(
            {
                "external_conversation_id": "conv-1",
                "external_message_id": "msg-1",
                "text": "hello",
                "token": "leak",
            }
        )


def test_manager_takeover_blocks_automatic_reply() -> None:
    conversation = Conversation(
        channel="synthetic",
        external_conversation_id="conv-takeover",
        status=ConversationStatus.OPEN.value,
        manager_takeover_at=datetime.now(timezone.utc),
    )
    assert conversation_allows_automatic_reply(conversation) is False


def test_handoff_and_closed_block_automatic_reply() -> None:
    handoff = Conversation(
        channel="synthetic",
        external_conversation_id="conv-handoff",
        status=ConversationStatus.HANDOFF.value,
        handoff_state=HandoffState.HUMAN_ACTIVE.value,
        manager_takeover_at=None,
    )
    closed = Conversation(
        channel="synthetic",
        external_conversation_id="conv-closed",
        status=ConversationStatus.CLOSED.value,
        handoff_state=HandoffState.BOT_ACTIVE.value,
        manager_takeover_at=None,
    )
    open_dialog = Conversation(
        channel="synthetic",
        external_conversation_id="conv-open",
        status=ConversationStatus.OPEN.value,
        handoff_state=HandoffState.BOT_ACTIVE.value,
        manager_takeover_at=None,
    )
    assert conversation_allows_automatic_reply(handoff) is False
    assert conversation_allows_automatic_reply(closed) is False
    assert conversation_allows_automatic_reply(open_dialog) is True


def test_outbox_has_no_sent_status() -> None:
    assert not hasattr(DeliveryStatus, "SENT")
    assert "SENT" not in {item.value for item in DeliveryStatus}
    assert DeliveryStatus.PENDING.value in {item.value for item in DeliveryStatus}
    assert DeliveryStatus.CANCELLED.value in {item.value for item in DeliveryStatus}
    assert DestinationType.INTERNAL_DRAFT.value in {
        item.value for item in DestinationType
    }
    assert DestinationType.SYNTHETIC_OUTBOUND.value in {
        item.value for item in DestinationType
    }


def test_inbound_service_has_no_client_sender() -> None:
    assert_no_client_outbound_path()


def test_repository_and_service_modules_have_no_transport_send() -> None:
    roots = [
        _REPO_ROOT / "app" / "services",
        _REPO_ROOT / "app" / "repositories",
        _REPO_ROOT / "app" / "models",
    ]
    banned = (
        "def send_to_client",
        "def publish_outbound",
        "transport_send(",
        "DeliveryStatus.SENT",
        'delivery_status="SENT"',
        "delivery_status='SENT'",
    )
    scanned = 0
    for package in roots:
        assert package.is_dir(), f"missing package root: {package}"
        for path in package.rglob("*.py"):
            scanned += 1
            text = path.read_text(encoding="utf-8")
            for token in banned:
                assert token not in text, f"{path}: contains {token}"
    assert scanned > 0


def test_session_scope_is_explicit_unit_of_work() -> None:
    source = inspect.getsource(session_scope)
    assert "session.begin()" in source
    assert "asynccontextmanager" in inspect.getsource(session_module)
    app_root = _REPO_ROOT / "app"
    for path in (app_root / "services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "commit(" not in text
        assert "rollback(" not in text
    for path in (app_root / "repositories").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert ".commit(" not in text
        assert ".rollback(" not in text


def test_engine_defaults_to_read_committed() -> None:
    assert _DEFAULT_ISOLATION_LEVEL == "READ COMMITTED"
    source = inspect.getsource(session_module.create_engine)
    assert "isolation_level=_DEFAULT_ISOLATION_LEVEL" in source
    assert "READ COMMITTED" in inspect.getsource(session_module)


def test_metadata_contains_all_check_and_unique_constraints() -> None:
    dialect = postgresql.dialect()
    rendered = "\n".join(
        str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    )
    expected_checks = {
        "ck_conversations_channel",
        "ck_conversations_status",
        "ck_conversations_ownership",
        "ck_conversations_context_version_nonnegative",
        "ck_inbox_channel",
        "ck_inbox_direction",
        "ck_inbox_message_type",
        "ck_inbox_processing_status",
        "ck_outbox_destination_type",
        "ck_outbox_delivery_status",
        "ck_ingress_channel",
        "ck_ingress_event_type",
        "ck_ingress_status",
        "ck_ingress_attempt_count_nonnegative",
        "ck_ingress_max_attempts_positive",
        "ck_ingress_lease_version_nonnegative",
        "ck_reply_plans_plan_type",
        "ck_reply_plans_status",
        "ck_amocrm_mirror_job_type",
        "ck_amocrm_mirror_subject_kind",
        "ck_amocrm_mirror_status",
        "ck_amocrm_mirror_context_version_nonnegative",
        "ck_worker_heartbeats_loop_name",
        "ck_worker_heartbeats_consecutive_failures_nonnegative",
        "ck_worker_heartbeats_failure_consistency",
    }
    expected_uniques = {
        "uq_conversations_channel_external_id",
        "uq_inbox_channel_external_message_id",
        "uq_outbox_source_inbox_destination",
        "uq_outbox_idempotency_key",
        "uq_outbox_reply_plan_destination",
        "uq_ingress_channel_external_event_id",
        "uq_reply_plans_conversation_context_version",
        "uq_amocrm_mirror_key",
    }
    for name in expected_checks | expected_uniques:
        assert name in rendered, f"missing constraint in metadata DDL: {name}"

    check_names: set[str] = set()
    unique_names: set[str] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and constraint.name:
                check_names.add(constraint.name)
            if isinstance(constraint, UniqueConstraint) and constraint.name:
                unique_names.add(constraint.name)
    assert expected_checks <= check_names
    assert expected_uniques <= unique_names
    assert (
        "delivery_status IN ('PENDING', 'PROCESSING', 'ADMITTED', "
        "'DELIVERED', 'FAILED', 'DEAD', 'CANCELLED')"
    ) in rendered
    assert "destination_type IN ('INTERNAL_DRAFT', 'SYNTHETIC_OUTBOUND')" in rendered
    assert "'SENT'" not in rendered
    assert "delivery_status IN ('PENDING', 'CANCELLED', 'SENT')" not in rendered
