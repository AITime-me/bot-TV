"""Architectural boundaries for booking self-booking (CURSOR-19/20).

Scans production ``app/`` sources only (not tests):
- ``decide_booking_dialog`` may be defined in booking_dialog_policy and called
  only from booking_eligibility_flow;
- ``BookingEligibilityFlowService`` may be defined in booking_eligibility_flow
  and composed only in ``app/main.py`` / ``app/services/worker_runtime.py``;
- application callers (inbound/reply) must use ``BookingFlowService``, not
  eligibility flow or dialog policy;
- ``application.state`` must not publish raw eligibility client/flow attributes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = _REPO_ROOT / "app"

_POLICY_DEF = Path("app/core/booking_dialog_policy.py")
_ELIGIBILITY_FLOW = Path("app/services/booking_eligibility_flow.py")
_BOOKING_FLOW = Path("app/services/booking_flow.py")
_BOOKING_SYNTHETIC = Path("app/services/booking_synthetic.py")
_MAIN = Path("app/main.py")
_WORKER_RUNTIME = Path("app/services/worker_runtime.py")
_REPLY_OUTBOUND = Path("app/services/reply_outbound.py")
_INBOUND = Path("app/services/inbound.py")

_DECIDE_ALLOWED = {_POLICY_DEF, _ELIGIBILITY_FLOW}
_ELIGIBILITY_FLOW_SERVICE_ALLOWED = {_ELIGIBILITY_FLOW, _MAIN, _WORKER_RUNTIME}


def _app_python_files() -> list[Path]:
    return sorted(_APP_ROOT.rglob("*.py"))


def _rel(path: Path) -> Path:
    return path.relative_to(_REPO_ROOT)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_decide_booking_dialog_only_from_eligibility_flow() -> None:
    offenders: list[str] = []
    for path in _app_python_files():
        rel = _rel(path)
        text = _source(path)
        if "decide_booking_dialog" not in text:
            continue
        if rel not in _DECIDE_ALLOWED:
            offenders.append(rel.as_posix())
            continue
        if rel == _ELIGIBILITY_FLOW:
            tree = ast.parse(text, filename=str(path))
            imported = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module == "app.core.booking_dialog_policy":
                        for alias in node.names:
                            if alias.name == "decide_booking_dialog":
                                imported = True
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "decide_booking_dialog":
                        imported = True
            assert imported, "eligibility flow must import/call decide_booking_dialog"
    assert offenders == [], (
        "decide_booking_dialog referenced outside allowed modules: "
        + ", ".join(offenders)
    )


def test_booking_eligibility_flow_service_not_used_by_app_callers() -> None:
    """Only definition + composition roots may name BookingEligibilityFlowService."""

    offenders: list[str] = []
    for path in _app_python_files():
        rel = _rel(path)
        text = _source(path)
        if "BookingEligibilityFlowService" not in text:
            continue
        if rel not in _ELIGIBILITY_FLOW_SERVICE_ALLOWED:
            offenders.append(rel.as_posix())
    assert offenders == [], (
        "BookingEligibilityFlowService used outside composition root: "
        + ", ".join(offenders)
    )


def test_booking_flow_consumer_does_not_import_eligibility_flow_class() -> None:
    text = _source(_REPO_ROOT / _BOOKING_FLOW)
    assert "BookingEligibilityFlowService" not in text
    assert "from app.services.booking_eligibility_flow" not in text
    assert "import app.services.booking_eligibility_flow" not in text
    assert "decide_booking_dialog" not in text
    assert "booking_dialog_policy" not in text


def test_inbound_and_reply_use_booking_flow_not_policy() -> None:
    inbound = _source(_REPO_ROOT / _INBOUND)
    reply = _source(_REPO_ROOT / _REPLY_OUTBOUND)
    bridge = _source(_REPO_ROOT / _BOOKING_SYNTHETIC)
    for label, text in (
        ("inbound", inbound),
        ("reply_outbound", reply),
        ("booking_synthetic", bridge),
    ):
        assert "decide_booking_dialog" not in text, label
        assert "BookingEligibilityFlowService" not in text, label
        assert "booking_dialog_policy" not in text, label
    assert "BookingFlowService" in reply
    assert "build_synthetic_outbound_payload" in reply
    assert "asyncio.to_thread" in reply
    assert "_booking_phase1_prepare" in reply
    assert "_booking_phase2_finalize" in reply
    assert "client_reply_plan_payload" in inbound
    assert "resolve_booking_outbound_fields" in bridge



def test_app_state_publishes_only_booking_flow_not_raw_eligibility() -> None:
    main_text = _source(_REPO_ROOT / _MAIN)
    assert "application.state.booking_flow" in main_text
    assert "application.state.booking_eligibility_client" not in main_text
    assert "application.state.booking_eligibility_flow" not in main_text

    offenders: list[str] = []
    for path in _app_python_files():
        rel = _rel(path)
        text = _source(path)
        for banned in (
            "application.state.booking_eligibility_client",
            "application.state.booking_eligibility_flow",
            "state.booking_eligibility_client",
            "state.booking_eligibility_flow",
        ):
            if banned in text:
                offenders.append(f"{rel.as_posix()}:{banned}")
    assert offenders == [], "raw eligibility leaked on app.state: " + ", ".join(
        offenders
    )


def test_create_app_never_assigns_none_booking_flow() -> None:
    main_text = _source(_REPO_ROOT / _MAIN)
    assert "BookingFlowService(None)" in main_text
    assert "application.state.booking_flow = resolved_booking_flow" in main_text
    tree = ast.parse(main_text, filename="app/main.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "booking_flow"
                and isinstance(node.value, ast.Constant)
                and node.value.value is None
            ):
                pytest.fail("application.state.booking_flow must not be assigned None")


def test_worker_runtime_composes_booking_flow_without_app_state() -> None:
    text = _source(_REPO_ROOT / _WORKER_RUNTIME)
    assert "build_booking_flow_for_worker" in text
    assert "BookingFlowService" in text
    assert "application.state" not in text
    assert "ReplyPlanWorker(" in text
    assert "booking_flow=" in text
    # Do not read FastAPI app.state; docstring may mention the phrase.
    tree = ast.parse(text, filename="app/services/worker_runtime.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "state":
            value = node.value
            if isinstance(value, ast.Name) and value.id in {"app", "application"}:
                pytest.fail("worker_runtime must not access app.state")
            if isinstance(value, ast.Attribute) and value.attr in {"app", "application"}:
                pytest.fail("worker_runtime must not access app.state")


_AVAILABILITY_MODULES = (
    Path("app/core/booking_availability_remote.py"),
    Path("app/core/booking_availability_http.py"),
)

_BANNED_HTTP_LIBS = ("httpx", "requests", "aiohttp", "urllib3")
_BANNED_DB_TOKENS = (
    "prisma",
    "create_engine",
    "sessionmaker",
    "async_sessionmaker",
    "sqlalchemy.orm",
)


def test_availability_client_uses_stdlib_s2s_only() -> None:
    for rel in _AVAILABILITY_MODULES:
        text = _source(_REPO_ROOT / rel)
        for banned in _BANNED_HTTP_LIBS:
            assert banned not in text, f"{rel}: banned HTTP lib {banned}"
        assert "S2sHttpTransport" in text or rel.name.endswith("_remote.py")
        for banned in _BANNED_DB_TOKENS:
            assert banned not in text, f"{rel}: banned DB token {banned}"


def test_availability_not_wired_to_live_channels_or_synthetic_flow() -> None:
    """CURSOR-22 is transport-only; no inbound/outbound/synthetic semantic changes."""

    for rel in (
        _INBOUND,
        _REPLY_OUTBOUND,
        _BOOKING_SYNTHETIC,
        _BOOKING_FLOW,
        _ELIGIBILITY_FLOW,
        _MAIN,
        _WORKER_RUNTIME,
    ):
        text = _source(_REPO_ROOT / rel)
        assert "get_available_days" not in text, rel.as_posix()
        assert "get_available_slots" not in text, rel.as_posix()
        assert "BookingAvailabilityHttpClient" not in text, rel.as_posix()
        assert "booking_availability_http" not in text, rel.as_posix()


def test_availability_modules_have_no_booking_writes() -> None:
    write_markers = (
        "INSERT ",
        "UPDATE ",
        "DELETE ",
        "/api/booking/",
        "hold",
        "create_booking",
        "booking_create",
    )
    for rel in _AVAILABILITY_MODULES:
        text = _source(_REPO_ROOT / rel)
        lower = text.lower()
        for marker in write_markers:
            if marker.lower() == "hold":
                # allow words like threshold; ban booking hold semantics only.
                assert "booking_hold" not in lower
                assert "create_hold" not in lower
                continue
            assert marker.lower() not in lower, f"{rel}: write marker {marker}"
