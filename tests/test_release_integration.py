import json
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import UploadFile
import main
from api.frame import receive_frame
from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.frame_queue import FrameQueue
from services.frame_sink import OcrSink
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
        text = "Full body beyond preview " * 20
        with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "Title", "text": text}), \
             patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
             patch("services.frame_sink.save_ocr_document", return_value={"id": "remote-id"}), \
             patch("services.mqtt_service.publish") as publish:
            result = await receive_frame("paired-camera", UploadFile(BytesIO(b"\xff\xd8\xffimage")), sink, queue, registry)
            self.assertTrue(result["queued"])
            self.assertEqual(registry.status("paired-camera")["state"], "connected")
            await queue.stop()
            messages = [c.args[1] for c in publish.call_args_list if c.args[0] == "ocr/text"]
            self.assertEqual(messages, [{"text": text, "task_id": "paired-camera"}])
            await handler._handle(SimpleNamespace(payload=json.dumps(messages[0]).encode(), retain=False))
            self.assertEqual(store.get_by_id("doc_001")["content"], text.strip())
            self.assertEqual(broadcast.await_count, 2)
            await sink.handle("paired-camera", b"image")
            self.assertEqual(len([c for c in publish.call_args_list if c.args[0] == "ocr/text"]), 1)

    async def test_routes_and_lifecycle_preserve_release(self):
        paths = [route.path for route in main.app.routes]
        for path in ["/", "/viewer", "/camera", "/frame", "/pair", "/session/{session_id}/status", "/ws/dashboard"]:
            self.assertEqual(paths.count(path), 1)
        store = MetaStore(":memory:")
        with patch("main.MetaStore", return_value=store), \
             patch.object(MqttHandler, "start", new_callable=AsyncMock) as start, \
             patch.object(MqttHandler, "stop", new_callable=AsyncMock) as stop, \
             patch("main.mqtt_service.close") as close:
            async with main.app.router.lifespan_context(main.app):
                self.assertEqual(store.get_count(), 0)
            start.assert_awaited_once()
            stop.assert_awaited_once()
            close.assert_called_once()

    async def test_failed_storage_does_not_publish_body(self):
        with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "T", "text": "text"}), \
             patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
             patch("services.frame_sink.save_ocr_document", side_effect=RuntimeError("store failed")), \
             patch("services.mqtt_service.publish") as publish:
            with self.assertRaises(RuntimeError):
                await OcrSink().handle("test", b"image")
            self.assertFalse(any(c.args[0] == "ocr/text" for c in publish.call_args_list))
