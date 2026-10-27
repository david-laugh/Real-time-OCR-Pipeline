"""모바일 카메라에서 올라온 프레임을 처리하는 Sink.

POST /frame 라우터(api/frame.py)는 `get_frame_sink()`가 돌려준 객체의
`handle(session_id, jpeg_bytes)`만 호출한다. 현재는 tmp_frames/에 저장만 하는
목업(LocalFolderSink)이 연결되어 있다.

[2번 팀원] OCR Sink 연결 방법
1. FrameSink와 같은 시그니처의 클래스를 만든다. 예) services/ocr_sink.py

       class OcrSink:
           async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict:
               text = await run_ocr(jpeg_bytes)   # OCR → 임베딩 → Supabase 저장
               return {"chars": len(text)}        # 응답 JSON의 "result"로 그대로 전달됨

2. 아래 get_frame_sink()가 OcrSink 인스턴스를 돌려주도록 한 줄만 바꾼다.
   (api/frame.py, 카메라 페이지는 수정할 필요 없음)
3. handle()에서 예외가 나면 /frame은 500을 돌려주고, 카메라 페이지는 그 프레임을
   버리고 2초 뒤 다음 프레임을 보낸다. 오래 걸리는 작업은 handle() 안에서
   백그라운드 큐로 넘기는 것을 권장(요청 타임아웃 8초).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_FRAMES_DIR = BASE_DIR / "tmp_frames"


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


_sink: FrameSink = LocalFolderSink()


def get_frame_sink() -> FrameSink:
    # OCR 연결 시 여기만 교체: return OcrSink()
    return _sink
