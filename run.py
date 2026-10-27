"""Single-worker server entry point with Windows MQTT event-loop support."""

import asyncio
import sys

import uvicorn


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config("main:app", host="0.0.0.0", port=8000))
    asyncio.run(server.serve())
