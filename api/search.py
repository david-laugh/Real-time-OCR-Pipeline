"""POST /api/rag/search: RAG question answering over stored OCR documents (pec response format).
(POST /api/search is the FTS5 search in api/routes.py; the response shapes are the same.)
GET /chat: standalone page that shows only the <rag-chat> widget (for testing).

Success: 200 {answer, sources:[{doc_id, title, preview, score}]}
Errors keep the same shape so the pec dashboard can always read data.answer:
    {answer: <user message>, sources: [], error: <code>} with HTTP 400/429/502/503
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from api.frame import SESSION_ID_RE
from services import rag_service

logger = logging.getLogger(__name__)

CHAT_HTML = Path(__file__).resolve().parent.parent / "static" / "chat" / "index.html"

# (HTTP status, error code, message shown in the chat)
INVALID_REQUEST = (400, "invalid_request", "질문 형식이 올바르지 않아요. 질문과 세션 ID를 확인해 주세요")
RATE_LIMITED = (429, "rate_limited", "사용량 한도에 걸렸어요. 잠시 후 다시 해 주세요")
SEARCH_FAILED = (502, "search_failed", "검색 중 오류가 났어요. 잠시 후 다시 해 주세요")
UNAVAILABLE = (503, "unavailable", "AI 서버가 바빠요. 잠시 후 다시 해 주세요")


def error_response(error: tuple) -> JSONResponse:
    status, code, message = error
    return JSONResponse(status_code=status, content={"answer": message, "sources": [], "error": code})


class SearchRoute(APIRoute):
    """Return body validation errors (FastAPI default 422 {detail}) as 400 in the search format.
    Scoped to this router only, so other routes keep FastAPI's default."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # loc/type only: the raw input (user question) is not logged
                logger.info("invalid search request: %s", [(e["loc"], e["type"]) for e in exc.errors()])
                return error_response(INVALID_REQUEST)

        return route_handler


router = APIRouter(tags=["rag-chat"], route_class=SearchRoute)


class SearchInput(BaseModel):
    query: str = Field(max_length=1000)
    limit: int = Field(default=5, ge=1, le=20)
    session_id: Optional[str] = None


@router.post("/api/rag/search")
async def search_documents(body: SearchInput) -> Any:
    if body.session_id is not None and not SESSION_ID_RE.match(body.session_id):
        return error_response(INVALID_REQUEST)
    # 원본 오류(키·쿼터 정보 포함)는 서버 로그에만 남기고 응답에는 담지 않는다
    try:
        return await rag_service.search(body.query, limit=body.limit, session_id=body.session_id)
    except rag_service.RateLimitedError:
        return error_response(RATE_LIMITED)
    except rag_service.UnavailableError:
        return error_response(UNAVAILABLE)
    except Exception:
        logger.exception("rag search failed")
        return error_response(SEARCH_FAILED)


@router.get("/chat", include_in_schema=False)
async def chat_page() -> FileResponse:
    return FileResponse(CHAT_HTML, headers={"Cache-Control": "no-cache"})
