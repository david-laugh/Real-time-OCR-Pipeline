"""Camera, QR pairing, OCR and MQTT dashboard application."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
logging.basicConfig(level=logging.INFO)

from api import camera, frame, pair, routes, session
from db.meta_store import MetaStore
from services import mqtt_service
from services.dedup import DedupFilter
from services.frame_queue import get_frame_queue
from services.mqtt_handler import MqttHandler
from services.pipeline import Pipeline
from services.search import SearchService


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = MetaStore(str(BASE_DIR / "data" / "documents.db"))
    pipeline = Pipeline(store, DedupFilter(enabled=False))
    routes.init(pipeline, SearchService(store), store)
    listener = MqttHandler(
        pipeline, routes.broadcast,
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
app.add_middleware(CORSMiddleware,
    allow_origins=["https://david-laugh.github.io"],
    allow_methods=["POST"], allow_headers=["*"])
for router in (camera.router, frame.router, session.router, pair.router, routes.router):
    app.include_router(router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(BASE_DIR / "static" / "dashboard.html")


@app.get("/viewer", include_in_schema=False)
async def viewer():
    return FileResponse(BASE_DIR / "static" / "viewer.html")
