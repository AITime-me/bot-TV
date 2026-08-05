"""Architectural boundaries for booking self-booking (CURSOR-19).

Scans production ``app/`` sources only (not tests):
- ``decide_booking_dialog`` may be defined in booking_dialog_policy and called
  only from booking_eligibility_flow;
- ``BookingEligibilityFlowService`` may be defined in booking_eligibility_flow
  and composed only in ``app/main.py`` — not used by other application modules
  in bypass of ``BookingFlowService``;
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
_MAIN = Path("app/main.py")

_DECIDE_ALLOWED = {_POLICY_DEF, _ELIGIBILITY_FLOW}
_ELIGIBILITY_FLOW_SERVICE_ALLOWED = {_ELIGIBILITY_FLOW, _MAIN}


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
    """Only definition + create_app composition may name BookingEligibilityFlowService."""

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
    # Explicit None kwarg must normalize, not assign None literally to state.
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
