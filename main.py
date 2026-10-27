"""
============================================================
[FILE] main.py
[역할] FastAPI 앱 진입점 — 서버 기동, 모듈 초기화, 라우터 등록
[실행] uvicorn main:app --reload --host 0.0.0.0 --port 8000
[합칠 때] QR 세션 생성 로직, 카메라 WebSocket 라우트 추가
============================================================
"""

from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.pipeline import Pipeline
from services.search import SearchService
from api.routes import router, init as init_routes

# ── [APP] ────────────────────────────────────────────
app = FastAPI(title="OCR Pipeline Dashboard")

# ── [INIT] 모듈 초기화 ──────────────────────────────
# [합칠 때] QR 매니저, 카메라 세션 매니저 여기서 초기화
meta_store = MetaStore(path="./data/documents.db")
dedup = DedupFilter(threshold=0.85, buffer_size=5, min_length=20)
pipeline = Pipeline(meta_store=meta_store, dedup=dedup)
search_service = SearchService(meta_store=meta_store)

init_routes(pipeline, search_service, meta_store)

# ── [ROUTES] ─────────────────────────────────────────
app.include_router(router)

# ── [STATIC] 정적 파일 서빙 ─────────────────────────
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    """대시보드 페이지"""
    return FileResponse(static_dir / "dashboard.html")


@app.get("/viewer")
async def viewer():
    """문서 뷰어 페이지 — 카드형 탐색 + 검색 + 상세 보기"""
    return FileResponse(static_dir / "viewer.html")


@app.get("/conveyor")
async def conveyor():
    """컨베이어 페이지 (제공 파일)"""
    path = static_dir / "conveyor.html"
    if path.exists():
        return FileResponse(path)
    return {"error": "conveyor.html not found — static/ 폴더에 넣어주세요"}


# ── [SHUTDOWN] ───────────────────────────────────────
@app.on_event("shutdown")
def shutdown():
    meta_store.close()
