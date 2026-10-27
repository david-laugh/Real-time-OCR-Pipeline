"""
============================================================
[FILE] services/mqtt_handler.py
[역할] MQTT 구독 → 파이프라인 처리 → WebSocket broadcast
[토픽] ocr/text (JSON: {text, task_id?})
============================================================
"""

import asyncio
import json
import logging
import sys
import ssl

import aiomqtt
from pydantic import ValidationError

from services.pipeline import Pipeline
from services.ocr_message import OcrMessage

logger = logging.getLogger("mqtt")

TOPIC = "ocr/text"


class MqttHandler:
    def __init__(
        self,
        pipeline: Pipeline,
        broadcast,
        host: str = "localhost",
        port: int = 1883,
        topic: str = TOPIC,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
    ):
        self._pipeline = pipeline
        self._broadcast = broadcast
        self._host = host
        self._port = port
        self._topic = topic
        self._username = username
        self._password = password
        self._tls_context = ssl.create_default_context() if tls else None
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._task and not self._task.done():
            return
        if sys.platform == "win32" and isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
            raise RuntimeError("MQTT requires SelectorEventLoop on Windows. Run: python run.py")
        self._task = asyncio.create_task(self._listen())
        logger.info("MQTT listener started → %s:%s topic=%s", self._host, self._port, self._topic)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("MQTT listener stopped")

    async def _listen(self):
        while True:
            try:
                async with aiomqtt.Client(
                    self._host, self._port, username=self._username, password=self._password,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    tls_context=self._tls_context,
                ) as client:
                    await client.subscribe(self._topic, qos=1)
                    logger.info("MQTT connected, subscribed to %s", self._topic)
                    async for msg in client.messages:
                        try:
                            await self._handle(msg)
                        except Exception:
                            logger.exception("MQTT message processing failed; continuing subscription")
            except aiomqtt.MqttError as e:
                logger.warning("MQTT connection lost (%s), reconnecting in 3s…", e)
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MQTT unexpected error: %s", e)
                await asyncio.sleep(3)

    async def _handle(self, msg):
        if msg.retain:
            logger.warning("MQTT retained message ignored")
            return
        try:
            payload = OcrMessage.model_validate(json.loads(msg.payload.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            logger.warning("MQTT invalid OCR payload; expected UTF-8 JSON with non-empty text and optional task_id")
            return

        text = payload.text
        task_id = payload.task_id

        result = self._pipeline.process(text, task_id=task_id)
        if result is None:
            logger.debug("MQTT text skipped (dedup)")
            return

        await self._broadcast({"type": "new_document", "data": result})
        await self._broadcast({"type": "stats", "data": {"total": self._pipeline.get_count()}})
        logger.info("MQTT → stored %s (task=%s, %d chars)", result["doc_id"], task_id, result["char_count"])
