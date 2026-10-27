"""POST /frame: 모바일 카메라 페이지가 보내는 JPEG 프레임 수신.

검증 후 바로 202로 응답하고, 실제 처리(sink.handle)는 FrameQueue가 백그라운드에서 한다.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from services.frame_queue import FrameQueue, get_frame_queue
from services.frame_sink import FrameSink, get_frame_sink
from services.session_registry import SessionRegistry, get_session_registry

router = APIRouter(tags=["mobile-camera"])

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_FRAME_BYTES = 5 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8\xff"


@router.post("/frame", status_code=202)
async def receive_frame(
    session_id: str = Form(...),
    frame: UploadFile = File(...),
    sink: FrameSink = Depends(get_frame_sink),
    queue: FrameQueue = Depends(get_frame_queue),
    registry: SessionRegistry = Depends(get_session_registry),
) -> dict:
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")

    data = await frame.read(MAX_FRAME_BYTES + 1)
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame too large")
    if not data.startswith(JPEG_MAGIC):
        raise HTTPException(status_code=415, detail="frame must be JPEG")

    registry.mark_frame(session_id)   # Pairing status: frame arrived, regardless of OCR outcome
    dropped = queue.submit(session_id, data, sink)
    return {"ok": True, "queued": True, "dropped": dropped}
