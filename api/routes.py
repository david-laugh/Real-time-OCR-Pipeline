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
============================================================
"""

import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.ocr_message import OcrMessage

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


TextInput = OcrMessage


class SearchInput(BaseModel):
    query: str
    limit: int = 5


@router.post("/api/process")
async def process_text(body: TextInput):
    result = _pipeline.process(body.text, task_id=body.task_id)
    if result is None:
        return {"status": "skipped", "reason": "duplicate or too short"}

    await broadcast({"type": "new_document", "data": result})
    await broadcast({"type": "stats", "data": {"total": _store.get_count()}})

    return {"status": "stored", **result}


async def broadcast(payload: dict):
    msg = json.dumps(payload)
    dead: list[WebSocket] = []
    for ws in _dashboard_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _dashboard_clients:
            _dashboard_clients.remove(ws)


@router.get("/api/tasks")
async def list_tasks():
    return _store.get_tasks()


@router.get("/api/tasks/{task_id}/docs")
async def get_task_docs(task_id: str):
    return _store.get_docs_by_task(task_id)


@router.get("/api/documents")
async def list_documents(limit: int = 20, task_id: str | None = None):
    return _store.get_recent(limit, task_id=task_id)


@router.get("/api/documents/{doc_id}")
async def get_document(doc_id: str):
    doc = _store.get_by_id(doc_id)
    if doc is None:
        return JSONResponse({"error": "not found"}, status_code=404)
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
