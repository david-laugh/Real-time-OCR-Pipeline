import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from api import camera, frame
from api import routes
from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.pipeline import Pipeline
from services.search import SearchService
from services.mqtt_handler import MqttHandler
from services import mqtt_service
from services.frame_queue import get_frame_queue

@asynccontextmanager
async def lifespan(app: FastAPI):
    store = MetaStore(path=str(BASE_DIR / "data" / "documents.db"))
    pipeline = Pipeline(store, DedupFilter(enabled=False))
    routes.init(pipeline, SearchService(store), store)
    listener = MqttHandler(
        pipeline=pipeline, broadcast=routes.broadcast,
        host=os.getenv("MQTT_HOST", "localhost"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        topic=os.getenv("MQTT_TOPIC", "ocr/text"),
        username=os.getenv("MQTT_USERNAME") or None,
        password=os.getenv("MQTT_PASSWORD") or None,
        tls=os.getenv("MQTT_TLS") == "1",
    )
    try:
        await listener.start()
        yield
    finally:
        await get_frame_queue().stop()
        mqtt_service.close()
        await listener.stop()
        store.close()

app = FastAPI(title="Real-time OCR Pipeline", lifespan=lifespan)

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
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(BASE_DIR / "static" / "dashboard.html")


@app.get("/viewer", include_in_schema=False)
async def viewer():
    return FileResponse(BASE_DIR / "static" / "viewer.html")
