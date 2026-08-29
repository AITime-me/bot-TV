"""Worker tick for control_plane_snapshot loop."""

from __future__ import annotations

import logging

from app.services.control_plane_snapshot_service import ControlPlaneSnapshotService

logger = logging.getLogger(__name__)

_ALLOWED_LOG_CODES: frozenset[str] = frozenset(
    {
        "CONTROL_PLANE_WORKER_TICK",
        "CONTROL_PLANE_WORKER_TICK_DONE",
    }
)


def _log(event: str) -> None:
    if type(event) is not str or event not in _ALLOWED_LOG_CODES:
        return
    try:
        logger.info("%s", event)
    except Exception:
        return


class ControlPlaneSnapshotWorker:
    def __init__(self, service: ControlPlaneSnapshotService) -> None:
        self._service = service

    async def tick(self) -> None:
        _log("CONTROL_PLANE_WORKER_TICK")
        await self._service.refresh()
        _log("CONTROL_PLANE_WORKER_TICK_DONE")
