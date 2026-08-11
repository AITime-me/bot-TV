"""CONTRACT-MODE-01 + M1: mode contract and live S2S read gate."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.config import BotMode, Settings
from app.core.booking_availability_http import (
    BookingAvailabilityHttpClient,
    BookingAvailabilityHttpError,
)
from app.core.booking_eligibility_factory import (
    build_booking_availability_client,
    build_booking_create_client,
    build_booking_eligibility_client,
    build_booking_flow_from_settings,
    build_booking_s2s_clients,
    rebind_booking_flow_to_runtime_settings,
)
from app.core.booking_eligibility_http import (
    BookingEligibilityHttpClient,
    BookingEligibilityHttpConfig,
)
from app.core.booking_types import BookingEligibilityOutcome, SelectedService
from app.core.mode_contract import (
    CONTROL_PLANE_TO_BOT_CORE_CAPABILITY,
    is_live_booking_s2s_read_allowed,
)
from app.core.outbound_policy import OutboundAction, is_automatic_outbound_allowed
from app.core.s2s_http_transport import S2sHttpRequest, S2sHttpResponse
from app.main import create_app
from app.services.booking_eligibility_flow import BookingEligibilityFlowService
from app.services.booking_flow import BookingFlowService
from app.services.worker_runtime import build_default_loop_specs

_REPO = Path(__file__).resolve().parents[1]
_TOKEN = "a" * 32
_URL = "https://eligibility.example"
_SERVICE = "11111111-1111-4111-8111-111111111111"
_MASTER = "22222222-2222-4222-8222-222222222222"


class _DenyTransport:
    def __init__(self) -> None:
        self.calls: list[S2sHttpRequest] = []

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        raise AssertionError(f"S2S network must not run: {request.method} {request.url}")


class _CountingTransport:
    def __init__(self, response: S2sHttpResponse) -> None:
        self.calls: list[S2sHttpRequest] = []
        self._response = response

    def request(self, request: S2sHttpRequest) -> S2sHttpResponse:
        self.calls.append(request)
        return self._response


def _settings(
    *,
    bot_mode: str = "OFF",
    emergency_lock: str = "true",
    configured: bool = True,
) -> Settings:
    env: dict[str, str] = {
        "BOT_MODE": bot_mode,
        "EMERGENCY_LOCK": emergency_lock,
    }
    if configured:
        env["BOOKING_ELIGIBILITY_BASE_URL"] = _URL
        env["BOOKING_ELIGIBILITY_BEARER_TOKEN"] = _TOKEN
    return Settings.from_env(env)


def _config() -> BookingEligibilityHttpConfig:
    return BookingEligibilityHttpConfig(
        base_url=_URL,
        bearer_token=_TOKEN,
        timeout_seconds=3.0,
        max_response_bytes=4096,
    )


def _eligibility_ok_response() -> S2sHttpResponse:
    import json

    body = json.dumps(
        {
            "ok": True,
            "outcome": "SELF_BOOKING_ALLOWED",
            "reasonCode": None,
            "selectedPairAllowed": True,
            "serviceOnlineInGeneral": True,
            "otherOnlineMasterCount": 0,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return S2sHttpResponse(
        status_code=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )


def test_control_plane_mapping_table_is_explicit() -> None:
    assert CONTROL_PLANE_TO_BOT_CORE_CAPABILITY == {
        "OFF": "OFF",
        "TEST": "EXPOSURE_ONLY_NOT_BOT_MODE",
        "HINTS": "HINTS",
        "DRAFT": "DRAFT",
        "AUTO": "AUTO_READ_MAX_UNTIL_WRITE_GATE",
    }
    assert "TEST" not in {mode.value for mode in BotMode}
    assert "AUTO" not in {mode.value for mode in BotMode}


@pytest.mark.parametrize(
    ("bot_mode", "emergency_lock", "allowed"),
    [
        ("OFF", "false", False),
        ("HINTS", "false", False),
        ("DRAFT", "false", False),
        ("AUTO_READ", "false", True),
        ("AUTO_WRITE", "false", True),
        ("OFF", "true", False),
        ("HINTS", "true", False),
        ("DRAFT", "true", False),
        ("AUTO_READ", "true", False),
        ("AUTO_WRITE", "true", False),
    ],
)
def test_live_s2s_read_matrix(
    bot_mode: str,
    emergency_lock: str,
    allowed: bool,
) -> None:
    settings = _settings(bot_mode=bot_mode, emergency_lock=emergency_lock)
    assert is_live_booking_s2s_read_allowed(settings) is allowed


def test_emergency_lock_precedes_auto_modes() -> None:
    for mode in ("AUTO_READ", "AUTO_WRITE"):
        settings = _settings(bot_mode=mode, emergency_lock="true")
        assert is_live_booking_s2s_read_allowed(settings) is False


@pytest.mark.parametrize("value", ["", "AUTO", "TEST", "UNKNOWN", "auto_read"])
def test_invalid_bot_mode_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="BOT_MODE must be one of"):
        Settings.from_env({"BOT_MODE": value, "EMERGENCY_LOCK": "false"})


def test_missing_settings_fail_closed_for_s2s_read() -> None:
    assert is_live_booking_s2s_read_allowed(None) is False
    assert is_live_booking_s2s_read_allowed(object()) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("bot_mode", ["OFF", "HINTS", "DRAFT"])
def test_factory_denies_read_clients_for_non_auto_modes(bot_mode: str) -> None:
    settings = _settings(bot_mode=bot_mode, emergency_lock="false")
    transport = _DenyTransport()
    assert build_booking_eligibility_client(settings, transport=transport) is None
    assert build_booking_availability_client(settings, transport=transport) is None
    clients = build_booking_s2s_clients(settings, transport=transport)
    assert clients.eligibility is None
    assert clients.availability is None
    # Write client construction is not the M1 read gate.
    assert clients.booking_create is not None
    assert build_booking_create_client(settings, transport=transport) is not None


@pytest.mark.parametrize("bot_mode", ["AUTO_READ", "AUTO_WRITE"])
def test_factory_allows_read_clients_when_unlocked(bot_mode: str) -> None:
    settings = _settings(bot_mode=bot_mode, emergency_lock="false")
    transport = _DenyTransport()
    eligibility = build_booking_eligibility_client(settings, transport=transport)
    availability = build_booking_availability_client(settings, transport=transport)
    assert isinstance(eligibility, BookingEligibilityHttpClient)
    assert isinstance(availability, BookingAvailabilityHttpClient)
    assert eligibility._settings is settings  # noqa: SLF001
    assert availability._settings is settings  # noqa: SLF001
    clients = build_booking_s2s_clients(settings, transport=transport)
    assert clients.eligibility is not None
    assert clients.availability is not None


@pytest.mark.parametrize("bot_mode", list(BotMode))
def test_factory_denies_read_clients_when_emergency_locked(bot_mode: BotMode) -> None:
    settings = _settings(bot_mode=bot_mode.value, emergency_lock="true")
    transport = _DenyTransport()
    assert build_booking_eligibility_client(settings, transport=transport) is None
    assert build_booking_availability_client(settings, transport=transport) is None
    clients = build_booking_s2s_clients(settings, transport=transport)
    assert clients.eligibility is None
    assert clients.availability is None


def test_http_client_defaults_deny_live_network() -> None:
    transport = _DenyTransport()
    eligibility = BookingEligibilityHttpClient(_config(), transport)
    result = eligibility.check_eligibility(SelectedService(service_id=_SERVICE))
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert transport.calls == []

    availability = BookingAvailabilityHttpClient(_config(), transport)
    with pytest.raises(BookingAvailabilityHttpError) as raised:
        availability.get_available_days(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
        )
    assert raised.value.code == "CONFIG_INVALID"
    assert transport.calls == []


def test_caller_controlled_live_read_flag_removed() -> None:
    """Former live_read_enabled=True bypass must not exist."""

    for cls in (BookingEligibilityHttpClient, BookingAvailabilityHttpClient):
        params = inspect.signature(cls.__init__).parameters
        assert "live_read_enabled" not in params
        with pytest.raises(TypeError):
            cls(_config(), _DenyTransport(), live_read_enabled=True)  # type: ignore[call-arg]


def test_direct_construction_off_mode_zero_network() -> None:
    transport = _DenyTransport()
    settings = _settings(bot_mode="OFF", emergency_lock="false")
    client = BookingEligibilityHttpClient(_config(), transport, settings=settings)
    result = client.check_eligibility(SelectedService(service_id=_SERVICE))
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert transport.calls == []

    availability = BookingAvailabilityHttpClient(_config(), transport, settings=settings)
    with pytest.raises(BookingAvailabilityHttpError):
        availability.get_available_days(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
        )
    assert transport.calls == []


@pytest.mark.parametrize("bot_mode", ["AUTO_READ", "AUTO_WRITE"])
def test_direct_construction_emergency_lock_zero_network(bot_mode: str) -> None:
    transport = _DenyTransport()
    settings = _settings(bot_mode=bot_mode, emergency_lock="true")
    client = BookingEligibilityHttpClient(_config(), transport, settings=settings)
    result = client.check_eligibility(SelectedService(service_id=_SERVICE))
    assert result.outcome is BookingEligibilityOutcome.SERVICE_UNAVAILABLE
    assert transport.calls == []

    availability = BookingAvailabilityHttpClient(_config(), transport, settings=settings)
    with pytest.raises(BookingAvailabilityHttpError):
        availability.get_available_days(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
        )
    assert transport.calls == []


def test_create_app_injected_permissive_http_client_runtime_off_zero_network() -> None:
    from datetime import datetime, timezone

    transport = _DenyTransport()
    permissive = BookingEligibilityHttpClient(
        _config(),
        transport,
        settings=_settings(bot_mode="AUTO_READ", emergency_lock="false"),
    )
    application = create_app(
        _settings(bot_mode="OFF", emergency_lock="false"),
        booking_eligibility_client=permissive,
    )
    booking = application.state.booking_flow
    assert isinstance(booking, BookingFlowService)
    result_flow = booking._eligibility_flow  # noqa: SLF001
    assert isinstance(result_flow, BookingEligibilityFlowService)
    bound = result_flow._client  # noqa: SLF001
    assert isinstance(bound, BookingEligibilityHttpClient)
    assert bound is not permissive
    assert bound._settings.bot_mode is BotMode.OFF  # noqa: SLF001

    decision = booking.resolve(
        SelectedService(service_id=_SERVICE),
        None,
        (),
        now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        include_alternatives=False,
    )
    assert decision is not None
    assert transport.calls == []


def test_worker_injected_flow_runtime_off_zero_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    transport = _DenyTransport()
    permissive_settings = _settings(bot_mode="AUTO_READ", emergency_lock="false")
    permissive_client = BookingEligibilityHttpClient(
        _config(), transport, settings=permissive_settings
    )
    permissive_flow = BookingFlowService(
        BookingEligibilityFlowService(permissive_client)
    )
    runtime = _settings(bot_mode="OFF", emergency_lock="false")

    rebound: list[BookingFlowService] = []
    real_rebind = rebind_booking_flow_to_runtime_settings

    def _spy(settings: Settings, booking_flow: object) -> object:
        out = real_rebind(settings, booking_flow)
        assert isinstance(out, BookingFlowService)
        rebound.append(out)
        return out

    monkeypatch.setattr(
        "app.services.worker_runtime.rebind_booking_flow_to_runtime_settings",
        _spy,
    )

    class _FakeSessionFactory:
        pass

    build_default_loop_specs(
        settings=runtime,
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        worker_id="unit-worker",
        booking_flow=permissive_flow,
    )
    assert len(rebound) == 1
    bound_flow = rebound[0]
    assert bound_flow is not permissive_flow
    inner = bound_flow._eligibility_flow._client  # noqa: SLF001
    assert isinstance(inner, BookingEligibilityHttpClient)
    assert inner._settings.bot_mode is BotMode.OFF  # noqa: SLF001

    decision = bound_flow.resolve(
        SelectedService(service_id=_SERVICE),
        None,
        (),
        now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc),
        include_alternatives=False,
    )
    assert decision is not None
    assert transport.calls == []


@pytest.mark.parametrize("bot_mode", ["AUTO_READ", "AUTO_WRITE"])
def test_auto_unlocked_allows_network_read(bot_mode: str) -> None:
    from app.core.booking_types import SelectedMaster

    transport = _CountingTransport(_eligibility_ok_response())
    settings = _settings(bot_mode=bot_mode, emergency_lock="false")
    client = BookingEligibilityHttpClient(_config(), transport, settings=settings)
    result = client.check_eligibility(
        SelectedService(service_id=_SERVICE),
        SelectedMaster(master_id=_MASTER),
    )
    assert result.outcome is BookingEligibilityOutcome.SELF_BOOKING_ALLOWED
    assert len(transport.calls) == 1


def test_eligibility_delegate_preserves_runtime_policy() -> None:
    transport = _DenyTransport()
    settings = _settings(bot_mode="OFF", emergency_lock="false")
    client = BookingEligibilityHttpClient(_config(), transport, settings=settings)
    with pytest.raises(BookingAvailabilityHttpError) as raised:
        client.get_available_days(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
        )
    assert raised.value.code == "CONFIG_INVALID"
    assert transport.calls == []

    allow_transport = _CountingTransport(
        S2sHttpResponse(
            status_code=200,
            headers={"Content-Type": "application/json", "Content-Length": "2"},
            body=b"{}",
        )
    )
    allow_settings = _settings(bot_mode="AUTO_READ", emergency_lock="false")
    allow_client = BookingEligibilityHttpClient(
        _config(), allow_transport, settings=allow_settings
    )
    # Malformed body fails after network — proves policy allowed the read.
    with pytest.raises(BookingAvailabilityHttpError):
        allow_client.get_available_days(
            service_id=_SERVICE,
            master_id=_MASTER,
            month="2026-08",
        )
    assert len(allow_transport.calls) == 1


def test_outbound_and_defaults_not_weakened() -> None:
    for mode in BotMode:
        for lock in (True, False):
            settings = Settings(bot_mode=mode, emergency_lock=lock)
            assert (
                is_automatic_outbound_allowed(settings, OutboundAction.SEND_MESSAGE)
                is False
            )
    defaults = Settings.from_env({})
    assert defaults.bot_mode is BotMode.OFF
    assert defaults.emergency_lock is True


def test_architecture_no_caller_controlled_live_read_bypass_in_app() -> None:
    app_root = _REPO / "app"
    offenders: list[str] = []
    for path in app_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "live_read_enabled" in text:
            offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []

    factory = (app_root / "core/booking_eligibility_factory.py").read_text(
        encoding="utf-8"
    )
    contract = (app_root / "core/mode_contract.py").read_text(encoding="utf-8")
    eligibility_http = (app_root / "core/booking_eligibility_http.py").read_text(
        encoding="utf-8"
    )
    availability_http = (app_root / "core/booking_availability_http.py").read_text(
        encoding="utf-8"
    )
    assert "is_live_booking_s2s_read_allowed" in factory
    assert "is_live_booking_s2s_read_allowed" in eligibility_http
    assert "is_live_booking_s2s_read_allowed" in availability_http
    assert "settings=settings" in factory
    assert "rebind_eligibility_client_to_runtime_settings" in factory
    assert "AUTO_READ_MAX_UNTIL_WRITE_GATE" in contract
    assert "EXPOSURE_ONLY_NOT_BOT_MODE" in contract
    # Create HTTP module must not grow a parallel mode gate that weakens writes.
    create_http = (app_root / "core/booking_create_http.py").read_text(encoding="utf-8")
    assert "is_live_booking_s2s_read_allowed" not in create_http
    assert "BOT_MODE" not in create_http
    assert "EMERGENCY_LOCK" not in create_http


def test_auto_write_read_allowed_create_gate_unchanged() -> None:
    settings = _settings(bot_mode="AUTO_WRITE", emergency_lock="false")
    assert is_live_booking_s2s_read_allowed(settings) is True
    assert build_booking_eligibility_client(settings, transport=_DenyTransport()) is not None
    # Write client still builds without M1 read coupling.
    assert build_booking_create_client(settings, transport=_DenyTransport()) is not None
    flow = build_booking_flow_from_settings(settings, transport=_DenyTransport())
    assert isinstance(flow, BookingFlowService)
