"""Real HTTP/MQTT/WebSocket smoke test; requires amqtt==0.11.3.

External OCR/cloud calls are mocked; application DB is isolated in memory.
Run from repository root: python -m tests.smoke_network is not required;
use python tests/smoke_network.py with the repository on PYTHONPATH.
"""
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def smoke():
    from amqtt.broker import Broker
    import uvicorn
    import websockets

    mqtt_port, http_port = free_port(), free_port()
    os.environ.update(MQTT_HOST="127.0.0.1", MQTT_PORT=str(mqtt_port),
                      MQTT_TOPIC="ocr/text", MQTT_TLS="0", MQTT_USERNAME="", MQTT_PASSWORD="")
    import main
    from db.meta_store import MetaStore
    from services.mqtt_handler import MqttHandler

    logging.getLogger().setLevel(logging.WARNING)
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{mqtt_port}"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=http_port, log_level="warning"))
    store = MetaStore(":memory:")
    received = asyncio.Event()
    original_handle = MqttHandler._handle

    async def tracked_handle(self, msg):
        await original_handle(self, msg)
        received.set()

    def request(path, body=None, content_type="application/json"):
        req = Request(f"http://127.0.0.1:{http_port}{path}", data=body,
                      headers={"Content-Type": content_type})
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    async def http(path, value=None):
        return await asyncio.to_thread(request, path, None if value is None else json.dumps(value).encode())

    with patch("main.MetaStore", return_value=store), patch.object(MqttHandler, "_handle", tracked_handle):
        server_task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.05)
            assert server.started, "HTTP server did not start"
            # Wait for a real subscription, not merely app startup.
            for _ in range(100):
                if broker._subscriptions.get("ocr/text"):
                    break
                await asyncio.sleep(0.05)
            assert broker._subscriptions.get("ocr/text"), "MQTT subscription not ready"
            for path in ["/", "/viewer", "/camera", "/static/camera/camera.js", "/openapi.json"]:
                status, body = await http(path)
                assert status == 200 and body, (path, status)
            print("PASS: server startup, dashboard/viewer/camera/static/OpenAPI HTTP 200")
            async with websockets.connect(f"ws://127.0.0.1:{http_port}/ws/dashboard") as ws:
                assert json.loads(await ws.recv())["type"] == "stats"
                status, body = await http("/api/process", {"text": "REST searchable integration", "task_id": "rest-test"})
                assert status == 200, body
                assert json.loads(await ws.recv())["type"] == "new_document"
                assert json.loads(await ws.recv())["type"] == "stats"
                status, _ = await http("/api/process", {"text": " "})
                assert status == 422
                print("PASS: REST save, validation, live WebSocket events")
                boundary = "ocr-smoke-boundary"
                def multipart(session, image):
                    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n{session}\r\n"
                            f"--{boundary}\r\nContent-Disposition: form-data; name=\"frame\"; filename=\"test.jpg\"\r\n"
                            "Content-Type: image/jpeg\r\n\r\n").encode() + image + f"\r\n--{boundary}--\r\n".encode()
                content_type = f"multipart/form-data; boundary={boundary}"
                status, _ = await asyncio.to_thread(request, "/frame", multipart("test", b"not-jpeg"), content_type)
                assert status == 415
                full_text = "Camera searchable body " * 20
                with patch("services.frame_sink.ocr_service.extract_text", return_value={"title": "Camera", "text": full_text}), \
                     patch("services.frame_sink.create_embedding", new=AsyncMock(return_value=[0.1])), \
                     patch("services.frame_sink.save_ocr_document", return_value={"id": "mock-cloud-id"}):
                    status, body = await asyncio.to_thread(request, "/frame", multipart("network-test", b"\xff\xd8\xffmock"), content_type)
                    assert status == 202, body
                    await asyncio.wait_for(received.wait(), timeout=10)
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert event["type"] == "new_document" and event["data"]["task_id"] == "network-test", event
                assert json.loads(await ws.recv())["data"]["total"] == 2
                status, body = await http("/api/tasks/network-test/docs")
                assert status == 200 and json.loads(body)[0]["content"] == full_text.strip()
                status, body = await http("/api/search", {"query": "Camera"})
                assert status == 200 and json.loads(body)["sources"], body
                print("PASS: multipart frame HTTP 202 -> real Paho/broker/aiomqtt -> full SQLite body -> WebSocket -> FTS search")
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=15)
            await broker.shutdown()
    print("PASS: clean server and broker shutdown; production DB untouched")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(smoke())
