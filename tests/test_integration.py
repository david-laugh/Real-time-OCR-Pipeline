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


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_to_mqtt_to_dashboard(self):
        store = MetaStore(":memory:")
        self.addCleanup(store.close)
        broadcast = AsyncMock()
        receiver = MqttHandler(Pipeline(store, DedupFilter(enabled=False)), broadcast)
        queue = FrameQueue()
        sink = OcrSink()
        text = "Complete OCR body " * 20
        with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "Title", "text": text}), \
             patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
             patch("services.frame_sink.save_ocr_document", return_value={"id": "remote-1"}), \
             patch("services.mqtt_service.publish") as publish:
            response = await receive_frame("camera-1", UploadFile(BytesIO(b"\xff\xd8\xffimage")), sink, queue)
            self.assertTrue(response["queued"])
            await queue.stop()
            documents = [c for c in publish.call_args_list if c.args[0] == "ocr/text"]
            self.assertEqual(len(documents), 1)
            payload = documents[0].args[1]
            self.assertEqual(payload, {"text": text, "task_id": "camera-1"})
            await receiver._handle(SimpleNamespace(payload=json.dumps(payload).encode(), retain=False))
            self.assertEqual(store.get_by_id("doc_001")["content"], text.strip())
            self.assertEqual(store.get_by_id("doc_001")["task_id"], "camera-1")
            self.assertEqual(broadcast.await_count, 2)
            await sink.handle("camera-1", b"image")
            self.assertEqual(len([c for c in publish.call_args_list if c.args[0] == "ocr/text"]), 1)

    async def test_storage_failure_does_not_publish_document(self):
        with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "Title", "text": "body"}), \
             patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
             patch("services.frame_sink.save_ocr_document", side_effect=RuntimeError("store failed")), \
             patch("services.mqtt_service.publish") as publish:
            with self.assertRaises(RuntimeError):
                await OcrSink().handle("camera-1", b"image")
            self.assertFalse(any(c.args[0] == "ocr/text" for c in publish.call_args_list))
            self.assertEqual(publish.call_args.args[1]["stage"], "failed")

    async def test_publish_failure_preserves_successful_storage(self):
        with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "Title", "text": "body"}), \
             patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
             patch("services.frame_sink.save_ocr_document", return_value={"id": "remote-1"}), \
             patch("services.mqtt_service.publish"), \
             patch("services.mqtt_service.publish_document", side_effect=RuntimeError("broker error")):
            result = await OcrSink().handle("camera-1", b"image")
            self.assertEqual(result["stage"], "stored")

    async def test_combined_app_routes_and_lifecycle(self):
        paths = [r.path for r in main.app.routes]
        for path in ["/", "/viewer", "/camera", "/frame", "/api/process", "/ws/dashboard"]:
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

    async def test_document_topic_override(self):
        from services import mqtt_service
        with patch.dict("os.environ", {"MQTT_TOPIC": "custom/text"}), patch.object(mqtt_service, "publish") as publish:
            mqtt_service.publish_document("session", "full text")
            publish.assert_called_once_with("custom/text", {"text": "full text", "task_id": "session"})
