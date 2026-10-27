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


RATE_LIMITED_MESSAGE = "사용량 한도에 걸렸어요. 잠시 후 다시 해 주세요"
FAILED_MESSAGE = "검색 중 오류가 났어요. 잠시 후 다시 해 주세요"


@router.post("/api/search")
async def search_documents(body: SearchInput) -> dict:
    if body.session_id is not None and not SESSION_ID_RE.match(body.session_id):
        raise HTTPException(status_code=400, detail="invalid session_id")
    # 원본 오류(키·쿼터 정보 포함)는 서버 로그에만 남기고 응답에는 담지 않는다
    try:
        return await rag_service.search(body.query, limit=body.limit, session_id=body.session_id)
    except rag_service.RateLimitedError as exc:
        raise HTTPException(status_code=429, detail=RATE_LIMITED_MESSAGE) from exc
    except Exception as exc:
        logger.exception("rag search failed")
        raise HTTPException(status_code=502, detail=FAILED_MESSAGE) from exc
