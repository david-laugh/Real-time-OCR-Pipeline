from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import camera, frame, pair, search, session

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Real-time OCR Pipeline")

# github.io의 카메라 페이지(루트 index.html)가 터널 주소의 /frame으로 전송할 수 있도록 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://david-laugh.github.io"],
    allow_methods=["POST"],
    allow_headers=["*"],
)

# --- 라우터 등록: 담당자별로 한 줄씩 추가 ---------------------------------
# 1번 (모바일 카메라): /camera, /frame
app.include_router(camera.router)
app.include_router(frame.router)
app.include_router(session.router)
app.include_router(pair.router)
# 2번 (OCR):
# 3번 (대시보드/RAG):
app.include_router(search.router)
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
