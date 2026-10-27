import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import numpy as np
from fastapi import UploadFile
import main
from api.frame import receive_frame
from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.frame_queue import FrameQueue
from services.frame_sink import CardJob, OcrSink
from services.mqtt_handler import MqttHandler
from services.pipeline import Pipeline
from services.session_registry import SessionRegistry


class ReleaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_pairing_and_full_body_dashboard(self):
        store = MetaStore(":memory:")
        self.addCleanup(store.close)
        broadcast = AsyncMock()
        handler = MqttHandler(Pipeline(store, DedupFilter(enabled=False)), broadcast)
        registry, queue, sink = SessionRegistry(), FrameQueue(), OcrSink()
        self.addAsyncCleanup(sink.stop)
        body = ("Full body beyond preview. " * 20).strip()
        card = CardJob("paired-camera", 1, 1, np.zeros((10, 10, 3), np.uint8), 0.0)
        document = {"title": "Title", "body": body, "sentences": ["Full body beyond preview."] * 20,
                    "raw_text": "", "confidence": 0.9}
        # 카드 검출/추적은 test_card_ocr_pipeline.py에서 검증한다. 여기서는 trigger된 카드 1장만 흘린다
        with patch("services.frame_sink.process_frame", side_effect=[[card], []]), \
             patch("services.frame_sink.ocr_service.read_card", return_value=document), \
             patch("services.mqtt_service.publish") as publish:
            result = await receive_frame("paired-camera", UploadFile(BytesIO(b"\xff\xd8\xffimage")), sink, queue, registry)
            self.assertTrue(result["queued"])
            self.assertEqual(registry.status("paired-camera")["state"], "connected")
            await queue.stop()
            await sink.stop()
            text = f"Title\n{body}"
            messages = [c.args[1] for c in publish.call_args_list if c.args[0] == "ocr/text"]
            self.assertEqual(messages, [{"text": text, "task_id": "paired-camera"}])
            await handler._handle(SimpleNamespace(payload=json.dumps(messages[0]).encode(), retain=False))
            self.assertEqual(store.get_by_id("doc_001")["content"], text)
            self.assertEqual(broadcast.await_count, 2)
            # 같은 카드가 계속 보여도(새 trigger 없음) 다시 발행하지 않는다
            await sink.handle("paired-camera", b"image")
            await sink.stop()
            self.assertEqual(len([c for c in publish.call_args_list if c.args[0] == "ocr/text"]), 1)

    async def test_routes_and_lifecycle_preserve_release(self):
        paths = [route.path for route in main.app.routes]
        for path in ["/", "/viewer", "/camera", "/frame", "/pair", "/session/{session_id}/status", "/ws/dashboard"]:
            self.assertEqual(paths.count(path), 1)
        store = MetaStore(":memory:")
        with patch("main.MetaStore", return_value=store), \
             patch.object(MqttHandler, "start", new_callable=AsyncMock) as start, \
             patch.object(MqttHandler, "stop", new_callable=AsyncMock) as stop, \
             patch("main.mqtt_service.close") as close, \
             patch("main.frame_sink.startup") as ocr_startup:
            async with main.app.router.lifespan_context(main.app):
                self.assertEqual(store.get_count(), 0)
            start.assert_awaited_once()
            stop.assert_awaited_once()
            close.assert_called_once()
            ocr_startup.assert_called_once()

    async def test_failed_ocr_does_not_publish_body(self):
        sink = OcrSink()
        card = CardJob("test", 1, 1, np.zeros((10, 10, 3), np.uint8), 0.0)
        with patch("services.frame_sink.process_frame", return_value=[card]), \
             patch("services.frame_sink.ocr_service.read_card", side_effect=RuntimeError("ocr failed")), \
             patch("services.mqtt_service.publish") as publish:
            await sink.handle("test", b"image")
            await sink.stop()
            self.assertFalse(any(c.args[0] == "ocr/text" for c in publish.call_args_list))
            self.assertEqual([c.args[1]["stage"] for c in publish.call_args_list], ["failed"])
