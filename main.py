from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Real-time OCR Pipeline")

# --- 라우터 등록: 담당자별로 한 줄씩 추가 ---------------------------------
# 1번 (모바일 카메라): /camera, /frame
# 2번 (OCR):
# 3번 (대시보드/RAG):
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
