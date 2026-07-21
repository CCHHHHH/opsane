"""Runtime state REST endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from shell_agent.web.runtime import get_runtime


router = APIRouter()


@router.get("/api/state")
async def get_state() -> dict:
    """Return the current target summary and execution counters."""
    rt = get_runtime()
    current_server = None
    if rt.servers:
        first = next(iter(rt.servers.values()))
        current_server = {
            "alias": first.alias,
            "host": first.host,
            "env": first.env,
        }
    return {
        "current_server": current_server,
        "stats": {"executed": 0, "failed": 0},
    }
