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
from math import hypot
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Set

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

# --- 카드 추적 / OCR 기준선 (ObjectCounter 방식: 중심점이 세로 기준선을 넘을 때 한 번만 OCR) ---
OCR_TRIGGER_X_RATIO = 0.5         # 기준선 x = 프레임 폭 × 이 값
TRACK_DIRECTION = "left"          # 카드 진행 방향: "left"(오른쪽→왼쪽) 또는 "right"
EDGE_MARGIN_RATIO = 0.02          # 카드가 좌우 경계에서 프레임 폭 × 이 값 이상 떨어져야 OCR (잘린 카드 제외)
IOU_MATCH_THRESHOLD = 0.3         # 직전 위치와 IoU가 이 이상이면 같은 카드
CENTER_DISTANCE_THRESHOLD = 0.3   # 또는 중심 거리가 프레임 폭 × 이 값 이하면 같은 카드 (프레임 간격 2초라 넉넉히)
BACKWARD_TOLERANCE_RATIO = 0.03   # 진행 반대 방향으로 프레임 폭 × 이 값 넘게 움직였으면 다른 카드
MAX_MISSING_FRAMES = 3            # 이 프레임 수를 넘게 안 보이면 track 삭제


class _Track:
    def __init__(self, track_id: int, box: ocr_service.Box) -> None:
        self.track_id = track_id
        self.box = box
        self.prev_center_x: Optional[float] = None
        self.missing = 0
        self.crossed = False  # 기준선을 넘었지만 아직 OCR 못 함(잘려 보이는 중)

    @property
    def center_x(self) -> float:
        return self.box[0] + self.box[2] / 2

    @property
    def center_y(self) -> float:
        return self.box[1] + self.box[3] / 2

    def update(self, box: ocr_service.Box) -> None:
        self.prev_center_x = self.center_x
        self.box = box
        self.missing = 0


class _SessionState:
    def __init__(self) -> None:
        self.tracks: Dict[int, _Track] = {}
        # OCR을 시작한 track. track_id는 재사용하지 않으므로 track이 지워질 때 함께 지운다
        # (다시 나타난 카드는 새 id로 기준선 너머에서 시작하므로 다시 crossing되지 않는다).
        self.processed_track_ids: Set[int] = set()
        self.next_track_id = 1


def _iou(a: ocr_service.Box, b: ocr_service.Box) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def _crossed_line(prev_x: float, cur_x: float, line_x: float) -> bool:
    if TRACK_DIRECTION == "left":
        return prev_x > line_x >= cur_x
    return prev_x < line_x <= cur_x


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
    """JPEG → 흰색 카드 검출/추적 → 기준선을 처음 넘은 카드만 crop → OCR → embedding → Supabase 저장.

    단계마다 MQTT(ocr/<session_id>/status)로 상태 발행. 카드 하나당 OCR은 한 번만 한다.
    """

    def __init__(self) -> None:
        self._last_text: Dict[str, str] = {}
        self._sessions: Dict[str, _SessionState] = {}

    async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        await self._notify(session_id, "received", message=f"Frame received ({len(jpeg_bytes)} bytes)")
        frame, boxes = await asyncio.to_thread(ocr_service.find_cards, jpeg_bytes)
        if frame is None:
            logger.warning("session=%s frame decode failed (%d bytes)", session_id, len(jpeg_bytes))
            await self._notify(session_id, "skipped", message="Frame could not be decoded")
            return {"stage": "skipped", "reason": "decode"}

        frame_w = frame.shape[1]
        due = self._select_cards(session_id, boxes, frame_w)
        if not due:
            await self._notify(session_id, "skipped", message="No new card crossed the OCR line")
            return {"stage": "skipped", "reason": "no_trigger"}

        results = []
        for track in due:
            crop = ocr_service.crop_card(frame, track.box)
            results.append(await self._process_card(session_id, track.track_id, crop))
        return {"stage": "processed", "cards": results}

    def _select_cards(self, session_id: str, boxes: List[ocr_service.Box], frame_w: int) -> List[_Track]:
        """track을 갱신하고, 이번 프레임에 OCR할 track을 processed로 표시해 돌려준다."""
        state = self._sessions.setdefault(session_id, _SessionState())
        logger.info("session=%s detected_cards=%d", session_id, len(boxes))
        seen = self._match_tracks(state, boxes, frame_w)

        line_x = frame_w * OCR_TRIGGER_X_RATIO
        margin = frame_w * EDGE_MARGIN_RATIO
        due: List[_Track] = []
        for track in seen:
            logger.debug("session=%s track=%d center_x=%.0f", session_id, track.track_id, track.center_x)
            processed = track.track_id in state.processed_track_ids
            if track.prev_center_x is not None and _crossed_line(track.prev_center_x, track.center_x, line_x):
                if processed:
                    logger.info("session=%s track=%d skipped: already processed", session_id, track.track_id)
                    continue
                track.crossed = True
                logger.info("session=%s track=%d crossed OCR line (center_x=%.0f)",
                            session_id, track.track_id, track.center_x)
            if not track.crossed or processed:
                continue
            x, _, w, _ = track.box
            if x < margin or x + w > frame_w - margin:
                logger.info("session=%s track=%d waiting: card cut off at frame edge", session_id, track.track_id)
                continue
            # OCR 전에 표시해 두어 같은 카드로 OCR이 두 번 시작되지 않게 한다
            state.processed_track_ids.add(track.track_id)
            due.append(track)
        return due

    @staticmethod
    def _match_tracks(state: _SessionState, boxes: List[ocr_service.Box], frame_w: int) -> List[_Track]:
        """IoU/중심 거리로 기존 track과 이어 붙이고(greedy), 이번 프레임에 보인 track을 돌려준다."""
        max_dist = frame_w * CENTER_DISTANCE_THRESHOLD
        tolerance = frame_w * BACKWARD_TOLERANCE_RATIO
        candidates = []
        for track in state.tracks.values():
            for i, box in enumerate(boxes):
                cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
                dx = cx - track.center_x
                iou = _iou(track.box, box)
                if iou < IOU_MATCH_THRESHOLD:
                    # 겹치지 않으면 거리로만 판단하되, 진행 반대 방향이면 뒤따라오던 다른 카드
                    backward = (dx > tolerance) if TRACK_DIRECTION == "left" else (dx < -tolerance)
                    if backward or hypot(dx, cy - track.center_y) > max_dist:
                        continue
                candidates.append((-iou, hypot(dx, cy - track.center_y), track.track_id, i))

        seen: List[_Track] = []
        used_tracks, used_boxes = set(), set()
        for _, _, track_id, i in sorted(candidates):
            if track_id in used_tracks or i in used_boxes:
                continue
            used_tracks.add(track_id)
            used_boxes.add(i)
            track = state.tracks[track_id]
            track.update(boxes[i])
            seen.append(track)

        for track_id in [t for t in state.tracks if t not in used_tracks]:
            track = state.tracks[track_id]
            track.missing += 1
            if track.missing > MAX_MISSING_FRAMES:
                del state.tracks[track_id]
                state.processed_track_ids.discard(track_id)

        for i, box in enumerate(boxes):
            if i not in used_boxes:
                track = _Track(state.next_track_id, box)
                state.next_track_id += 1
                state.tracks[track.track_id] = track
                seen.append(track)
        return seen

    async def _process_card(self, session_id: str, track_id: int, image: Any) -> dict[str, Any]:
        # 실패해도 이 카드는 processed로 남아 재시도하지 않는다 (기존처럼 예외는 frame_queue가 로그)
        stage = "ocr"
        try:
            logger.info("session=%s track=%d OCR started", session_id, track_id)
            result = await asyncio.to_thread(ocr_service.extract_text, image)
            logger.info("session=%s track=%d OCR completed (%d chars)", session_id, track_id, len(result["text"]))
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
            try:
                mqtt_service.publish_document(session_id, text)
            except Exception:
                logger.exception("ocr document publish failed (session=%s)", session_id)
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
