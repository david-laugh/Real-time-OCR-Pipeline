"""
============================================================
[FILE] api/routes.py
[역할] REST API + WebSocket 엔드포인트
[엔드포인트]
  POST /api/process     — 텍스트 수신 → 파이프라인 처리
  GET  /api/documents   — 저장된 문서 목록
  GET  /api/stats       — 통계
  POST /api/search      — FTS5 텍스트 검색
  WS   /ws/dashboard    — 대시보드 실시간 이벤트
[합칠 때] WS /ws/camera/{session_id} 라우트 추가
         GET /api/session (QR용 세션 정보) 추가
============================================================
"""

import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

router = APIRouter()

_pipeline = None
_search = None
_store = None
_dashboard_clients: list[WebSocket] = []


def init(pipeline, search_service, meta_store):
    global _pipeline, _search, _store
    _pipeline = pipeline
    _search = search_service
    _store = meta_store


class TextInput(BaseModel):
    text: str
    source_id: str | None = None


class SearchInput(BaseModel):
    query: str
    limit: int = 5


@router.post("/api/process")
async def process_text(body: TextInput):
    result = _pipeline.process(body.text)
    if result is None:
        return {"status": "skipped", "reason": "duplicate or too short"}

    msg = json.dumps({"type": "new_document", "data": result})
    for ws in list(_dashboard_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            _dashboard_clients.remove(ws)

    stats_msg = json.dumps({"type": "stats", "data": {"total": _store.get_count()}})
    for ws in list(_dashboard_clients):
        try:
            await ws.send_text(stats_msg)
        except Exception:
            _dashboard_clients.remove(ws)

    return {"status": "stored", **result}


@router.get("/api/documents")
async def list_documents(limit: int = 20):
    return _store.get_recent(limit)


@router.get("/api/documents/{doc_id}")
async def get_document(doc_id: str):
    doc = _store.get_by_id(doc_id)
    if doc is None:
        return {"error": "not found"}
    return doc


@router.get("/api/stats")
async def get_stats():
    return _store.get_stats()


@router.post("/api/search")
async def search_documents(body: SearchInput):
    return _search.search(body.query, limit=body.limit)


@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    await ws.accept()
    _dashboard_clients.append(ws)
    try:
        stats = {"type": "stats", "data": {"total": _store.get_count()}}
        await ws.send_text(json.dumps(stats))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _dashboard_clients:
            _dashboard_clients.remove(ws)
