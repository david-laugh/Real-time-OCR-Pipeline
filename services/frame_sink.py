"""모바일 카메라에서 올라온 프레임을 처리하는 Sink.

POST /frame 라우터(api/frame.py)는 `get_frame_sink()`가 돌려준 객체의
`handle(session_id, jpeg_bytes)`만 호출한다. 현재는 tmp_frames/에 저장만 하는
목업(LocalFolderSink)이 연결되어 있다.

[2번 팀원] OCR Sink 연결 방법
* handle()은 백그라운드에서 호출되며 오래 걸려도 휴대폰 전송에 영향 없음.
  /frame은 사진을 받자마자 202로 응답하고, 프레임은 services/frame_queue.py가
  세션별로 최신 2장까지만 보관하다가 handle()에 하나씩 넘긴다(같은 세션은 순차 처리).
  처리가 느리면 중간 프레임은 버려지고 항상 최신 프레임이 처리된다.

1. FrameSink와 같은 시그니처의 클래스를 만든다. 예) services/ocr_sink.py

       class OcrSink:
           async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict:
               text = await run_ocr(jpeg_bytes)   # OCR → 임베딩 → Supabase 저장
               return {"chars": len(text)}        # 반환값은 현재 사용하지 않음(로그/테스트용)

2. 아래 get_frame_sink()가 OcrSink 인스턴스를 돌려주도록 한 줄만 바꾼다.
   (api/frame.py, frame_queue.py, 카메라 페이지는 수정할 필요 없음)
3. handle()에서 난 예외는 로그만 남고 해당 프레임은 버려진다. 휴대폰에는 영향 없음.
4. 동기(블로킹) OCR 라이브러리를 쓰면 이벤트 루프가 멈추므로
   `await asyncio.to_thread(ocr_fn, jpeg_bytes)`처럼 스레드로 넘길 것.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Protocol

from db.supabase import save_ocr_document
from services import mqtt_service, ocr_service
from services.embedding_service import create_embedding

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_FRAMES_DIR = BASE_DIR / "tmp_frames"

# 같은 페이지를 계속 비추면 2초마다 같은 문서가 들어오므로,
# 세션의 직전 저장 텍스트와 이 비율 이상 같으면 저장하지 않는다.
DUPLICATE_SIMILARITY = 0.9
TEXT_PREVIEW_CHARS = 120


class FrameSink(Protocol):
    async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        ...


class LocalFolderSink:
    """tmp_frames/<session_id>/<시각>.jpg 로 저장만 하는 테스트용 Sink."""

    def __init__(self, root: Path = TMP_FRAMES_DIR) -> None:
        self.root = root

    async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        folder = self.root / session_id
        filename = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jpg"
        path = folder / filename
        await asyncio.to_thread(self._write, path, jpeg_bytes)
        return {"saved": f"{session_id}/{filename}"}

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


class OcrSink:
    """JPEG → OCR → embedding → Supabase 저장, 단계마다 MQTT(ocr/<session_id>/status)로 상태 발행."""

    def __init__(self) -> None:
        self._last_text: Dict[str, str] = {}

    async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        stage = "received"
        await self._notify(session_id, stage, message=f"Frame received ({len(jpeg_bytes)} bytes)")
        try:
            stage = "ocr"
            result = await asyncio.to_thread(ocr_service.extract_text, jpeg_bytes)
            title, text = result["title"], result["text"]
            if not text:
                await self._notify(session_id, "skipped", message="No readable text in frame")
                return {"stage": "skipped", "reason": "empty"}
            await self._notify(
                session_id, "ocr_completed", title=title, text=text,
                message=f"OCR completed ({len(text)} chars)",
            )

            if self._is_duplicate(session_id, text):
                await self._notify(
                    session_id, "skipped", title=title, text=text,
                    message="Same document as previous frame",
                )
                return {"stage": "skipped", "reason": "duplicate"}

            stage = "embedding"
            embedding = await create_embedding(text)
            await self._notify(
                session_id, "embedding_completed", title=title, text=text,
                message=f"Embedding created ({len(embedding)} dims)",
            )

            stage = "store"
            row = await asyncio.to_thread(
                save_ocr_document,
                session_id=session_id,
                title=title,
                text=text,
                embedding=embedding,
            )
            # 저장에 성공한 텍스트만 중복 기준으로 삼는다 (실패 시 다음 프레임에서 재시도)
            self._last_text[session_id] = text
            document_id = str(row.get("id", ""))
            logger.info("ocr document stored (session=%s, id=%s)", session_id, document_id)
            await self._notify(
                session_id, "stored", title=title, text=text,
                document_id=document_id, created_at=row.get("created_at"),
                message="Document stored successfully",
            )
            return {"stage": "stored", "document_id": document_id, "chars": len(text)}
        except Exception as exc:
            # traceback은 frame_queue가 logger.exception으로 남긴다
            logger.error("ocr pipeline failed at %s (session=%s): %r", stage, session_id, exc)
            await self._notify(session_id, "failed", message=f"{stage} failed: {exc}")
            raise

    def _is_duplicate(self, session_id: str, text: str) -> bool:
        last = self._last_text.get(session_id)
        if last is None:
            return False
        return SequenceMatcher(None, last, text).ratio() >= DUPLICATE_SIMILARITY

    @staticmethod
    async def _notify(
        session_id: str,
        stage: str,
        *,
        message: str,
        title: str = "",
        text: str = "",
        **extra: Any,
    ) -> None:
        event = {
            "type": "ocr_status",
            "stage": stage,
            "session_id": session_id,
            "title": title,
            "text_preview": text[:TEXT_PREVIEW_CHARS],
            "message": message,
            **extra,
        }
        # 상태 발행 실패는 OCR 저장에 영향을 주지 않는다
        try:
            mqtt_service.publish(mqtt_service.status_topic(session_id), event)
        except Exception:
            logger.exception("ocr status publish failed (session=%s)", session_id)


_sink: FrameSink = OcrSink()


def get_frame_sink() -> FrameSink:
    # OCR 연결 시 여기만 교체: return OcrSink()
    return _sink
