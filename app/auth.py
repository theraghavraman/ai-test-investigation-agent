"""Constant-time single-tenant API authentication; terminate TLS at a trusted proxy."""
import secrets

from fastapi import Depends, Header, HTTPException, status

from app.settings import Settings, get_settings


def require_api_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    supplied = authorization[7:]
    expected = settings.api_key.get_secret_value()
    if not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
