"""POST /api/search: RAG question answering over stored OCR documents (pec response format)."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.frame import SESSION_ID_RE
from services import rag_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rag-chat"])


class SearchInput(BaseModel):
    query: str = Field(max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    session_id: Optional[str] = None


@router.post("/api/search")
async def search_documents(body: SearchInput) -> dict:
    if body.session_id is not None and not SESSION_ID_RE.match(body.session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    try:
        return await rag_service.search(body.query, limit=body.limit, session_id=body.session_id)
    except Exception as exc:
        logger.exception("rag search failed")
        raise HTTPException(status_code=502, detail=f"search failed: {exc}") from exc
