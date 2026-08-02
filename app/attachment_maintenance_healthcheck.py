"""Docker healthcheck for attachment maintenance SUCCESS heartbeat.

Read-only CLI. No database access, no application config loader, no keyring,
and no maintenance cycle execution.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from app.core.attachment_maintenance_heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    HEARTBEAT_CONFIG_INVALID,
    HeartbeatError,
    STALE_SECONDS_ENV,
    parse_stale_seconds,
    read_and_validate_heartbeat,
)


def check_heartbeat(
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return 0 when heartbeat is fresh; 1 otherwise.

    Success prints nothing. Failure prints exactly one safe error code on stderr.
    """
    source = os.environ if environ is None else environ
    try:
        stale_seconds = parse_stale_seconds(source.get(STALE_SECONDS_ENV))
        read_and_validate_heartbeat(
            path=DEFAULT_HEARTBEAT_PATH if path is None else path,
            stale_seconds=stale_seconds,
        )
    except HeartbeatError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except Exception:
        print(HEARTBEAT_CONFIG_INVALID, file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return check_heartbeat()


if __name__ == "__main__":
    raise SystemExit(main())
