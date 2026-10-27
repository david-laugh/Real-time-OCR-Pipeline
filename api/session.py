"""GET /session/{session_id}/status: connection state polled by the QR pairing screen."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.frame import SESSION_ID_RE
from services.session_registry import SessionRegistry, get_session_registry

router = APIRouter(tags=["qr-pairing"])


@router.get("/session/{session_id}/status")
async def session_status(
    session_id: str,
    registry: SessionRegistry = Depends(get_session_registry),
) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    return registry.status(session_id)
