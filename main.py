from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import routes
from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.pipeline import Pipeline
from services.search import SearchService
from services.mqtt_handler import MqttHandler
from services import mqtt_service
from services.frame_queue import get_frame_queue
=======
from api import camera, frame, pair, session
>>>>>>> 97c52dc758519830eb8ffb261ff76bd4e7f6b870

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
# 2번 (OCR):
# 3번 (대시보드/RAG):
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
