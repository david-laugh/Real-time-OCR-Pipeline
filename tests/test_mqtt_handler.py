import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI

from api.routes import router, init
from db.meta_store import MetaStore
from services.dedup import DedupFilter
from services.mqtt_handler import MqttHandler
from services.pipeline import Pipeline
from services.search import SearchService


def message(value, retain=False):
    return SimpleNamespace(payload=json.dumps(value, ensure_ascii=False).encode("utf-8"), retain=retain)


class MqttTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MetaStore(":memory:")
        self.pipeline = Pipeline(self.store, DedupFilter(enabled=False))
        self.broadcast = AsyncMock()
        self.handler = MqttHandler(self.pipeline, self.broadcast)

    def tearDown(self):
        self.store.close()

    async def test_unicode_storage_and_events(self):
        await self.handler._handle(message({"text": "  한글 OCR\n두 번째 줄  ", "task_id": " scan-1 "}))
        doc = self.store.get_by_id("doc_001")
        self.assertEqual(doc["content"], "한글 OCR\n두 번째 줄")
        self.assertEqual(doc["task_id"], "scan-1")
        self.assertEqual(doc["char_count"], len(doc["content"]))
        events = [call.args[0] for call in self.broadcast.await_args_list]
        self.assertEqual(events[0]["type"], "new_document")
        self.assertEqual(events[1], {"type": "stats", "data": {"total": 1}})

    async def test_defaults_and_unknown_metadata(self):
        await self.handler._handle(message({"text": "OCR", "source_id": "ignored"}))
        doc = self.store.get_by_id("doc_001")
        self.assertEqual(doc["task_id"], "default")
        self.assertIsNone(doc["source_id"])

    async def test_invalid_messages_do_not_prevent_next_valid_message(self):
        invalid = [None, [], "text", 1, {}, {"text": None}, {"text": 123},
                   {"text": []}, {"text": " \n "}, {"text": "x", "task_id": None},
                   {"text": "x", "task_id": 1}, {"text": "x", "task_id": " "}]
        for value in invalid:
            with self.subTest(value=value):
                await self.handler._handle(message(value))
        for raw in (b"{", b"\xff"):
            await self.handler._handle(SimpleNamespace(payload=raw, retain=False))
        self.assertEqual(self.store.get_count(), 0)
        self.broadcast.assert_not_awaited()
        await self.handler._handle(message({"text": "valid"}))
        self.assertEqual(self.store.get_count(), 1)

    async def test_retained_replay_is_ignored(self):
        await self.handler._handle(message({"text": "old"}, retain=True))
        self.assertEqual(self.store.get_count(), 0)

    async def test_redelivery_is_not_idempotent(self):
        for _ in range(2):
            await self.handler._handle(message({"text": "same"}))
        self.assertEqual(self.store.get_count(), 2)

    async def test_processing_failure_keeps_subscription(self):
        async def messages():
            yield message({"text": "first"})
            yield message({"text": "second"})
            raise asyncio.CancelledError

        client = AsyncMock()
        client.__aenter__.return_value = client
        client.messages = messages()
        original = self.handler._handle
        calls = 0

        async def fail_once(msg):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated storage failure")
            await original(msg)

        with patch("services.mqtt_handler.aiomqtt.Client", return_value=client), patch.object(self.handler, "_handle", side_effect=fail_once):
            with self.assertRaises(asyncio.CancelledError):
                await self.handler._listen()
        client.subscribe.assert_awaited_once_with("ocr/text", qos=1)
        self.assertEqual(self.store.get_count(), 1)

    async def test_rest_uses_same_validation(self):
        app = FastAPI()
        init(self.pipeline, SearchService(self.store), self.store)
        app.include_router(router)
        async def post(body):
            sent = []
            receive = AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})

            async def send(event):
                sent.append(event)

            await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                       "method": "POST", "scheme": "http", "path": "/api/process",
                       "raw_path": b"/api/process", "query_string": b"", "root_path": "",
                       "headers": [(b"content-type", b"application/json")],
                       "client": ("127.0.0.1", 1234), "server": ("test", 80)}, receive, send)
            return sent[0]["status"], json.loads(sent[1]["body"])

        for body in ({"text": " "}, {"text": 1}, {"text": "ok", "task_id": None}):
            status, _ = await post(body)
            self.assertEqual(status, 422)
        status, result = await post({"text": "REST OCR"})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "stored")


if __name__ == "__main__":
    unittest.main()
