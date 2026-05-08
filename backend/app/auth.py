from __future__ import annotations

import os

from fastapi import Header, HTTPException, status


def auth_enabled() -> bool:
    return os.getenv("AIXION_AUTH_ENABLED", "false").lower() == "true"


def expected_api_key() -> str:
    return os.getenv("AIXION_API_KEY", "")


def require_api_key(x_aixion_api_key: str | None = Header(default=None)) -> None:
    """MVP API-key guard.

    Local development defaults to auth disabled. Production-like runs should set:

    AIXION_AUTH_ENABLED=true
    AIXION_API_KEY=<strong random token>
    """
    if not auth_enabled():
        return

    configured_key = expected_api_key()
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Auth is enabled but AIXION_API_KEY is not configured.",
        )

    if x_aixion_api_key != configured_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Aixion API key.",
        )
