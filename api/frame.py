"""POST /frame: 모바일 카메라 페이지가 보내는 JPEG 프레임 수신."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from services.frame_sink import FrameSink, get_frame_sink

router = APIRouter(tags=["mobile-camera"])

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FRAME_BYTES = 5 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"


@router.post("/frame")
async def receive_frame(
    session_id: str = Form(...),
    frame: UploadFile = File(...),
    sink: FrameSink = Depends(get_frame_sink),
) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")

    data = await frame.read(MAX_FRAME_BYTES + 1)
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame too large")
    if not data.startswith(JPEG_MAGIC):
        raise HTTPException(status_code=415, detail="frame must be JPEG")

    result = await sink.handle(session_id, data)
    return {"ok": True, "result": result}
