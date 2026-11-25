"""One structured log line per tool call and per authentication failure.

Never logged: the API key, any Authorization header, credentials, or anything
from an event or a query. A tool call records its name, outcome, duration and
how many items it touched - enough to see what the agent did, not what it read.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

_log = logging.getLogger("caldav-mcp.audit")


def log_auth_failure(client_ip: str, reason: str) -> None:
    """Record a rejected request."""
    _log.warning(
        json.dumps({"event": "auth", "success": False, "client_ip": client_ip, "reason": reason, "ts": time.time()})
    )


def log_operation(tool: str, status: str, duration_ms: float, **detail: Any) -> None:
    """Record one tool call."""
    entry = {"event": "tool", "tool": tool, "status": status, "duration_ms": round(duration_ms, 1), "ts": time.time()}
    entry.update({k: v for k, v in detail.items() if v is not None})
    _log.info(json.dumps(entry))
