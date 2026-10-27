"""/frame이 받은 프레임을 세션별로 모아 두었다가 백그라운드에서 sink로 넘기는 큐.

- 세션마다 대기 중인 프레임은 최신 MAX_PENDING_PER_SESSION장만 유지 (오래된 것부터 버림)
- 세션마다 워커 하나가 대기 프레임을 순서대로 sink.handle()에 넘긴다
- 처리 중인 프레임은 "대기 중" 개수에 포함되지 않는다
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque, Dict, Tuple

from services.frame_sink import FrameSink

logger = logging.getLogger(__name__)

MAX_PENDING_PER_SESSION = 2


class FrameQueue:
    def __init__(self, max_pending: int = MAX_PENDING_PER_SESSION) -> None:
        self.max_pending = max_pending
        self._pending: Dict[str, Deque[Tuple[FrameSink, bytes]]] = {}
        self._workers: Dict[str, asyncio.Task] = {}

    def submit(self, session_id: str, jpeg_bytes: bytes, sink: FrameSink) -> int:
        """프레임을 넣고 바로 반환한다. 버려진 오래된 프레임 수를 돌려준다."""
        queue = self._pending.setdefault(session_id, deque())
        queue.append((sink, jpeg_bytes))
        dropped = 0
        while len(queue) > self.max_pending:
            queue.popleft()
            dropped += 1

        if session_id not in self._workers:
            self._workers[session_id] = asyncio.create_task(self._drain(session_id))
        return dropped

    async def _drain(self, session_id: str) -> None:
        while True:
            queue = self._pending.get(session_id)
            if not queue:
                # 확인과 정리 사이에 await가 없으므로 submit()과 경합하지 않는다
                self._pending.pop(session_id, None)
                self._workers.pop(session_id, None)
                return
            sink, jpeg_bytes = queue.popleft()
            try:
                await sink.handle(session_id, jpeg_bytes)
            except Exception:
                logger.exception("frame sink failed (session=%s)", session_id)

    def pending_count(self, session_id: str) -> int:
        return len(self._pending.get(session_id, ()))

    async def stop(self) -> None:
        if self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)


_queue = FrameQueue()


def get_frame_queue() -> FrameQueue:
    return _queue
