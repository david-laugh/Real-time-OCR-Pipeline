"""모바일 카메라에서 올라온 프레임을 처리하는 Sink.

POST /frame 라우터(api/frame.py)는 `get_frame_sink()`가 돌려준 객체의
`handle(session_id, jpeg_bytes)`만 호출한다. 현재는 OcrSink가 연결되어 있다.

* handle()은 백그라운드에서 호출되며 오래 걸려도 휴대폰 전송에 영향 없음.
  /frame은 사진을 받자마자 202로 응답하고, 프레임은 services/frame_queue.py가
  세션별로 최신 2장까지만 보관하다가 handle()에 하나씩 넘긴다(같은 세션은 순차 처리).
  처리가 느리면 중간 프레임은 버려지고 항상 최신 프레임이 처리된다.
* handle()에서 난 예외는 로그만 남고 해당 프레임은 버려진다. 휴대폰에는 영향 없음.
* 동기(블로킹) 라이브러리(OpenCV, RapidOCR)는 asyncio.to_thread()로 이벤트 루프 밖에서 실행한다.

OcrSink 흐름 (세션마다 독립된 SessionState)
  handle()       JPEG → 카드 검출(긴 세로 edge + 카드 비율) → 추적 → trigger 판단   [to_thread]
                 PARTIAL → FULLY_VISIBLE 이후 중심점이 trigger 선을 넘은 track만
                 processed=True로 표시하고 카드 crop을 세션 OCR 큐에 넣은 뒤 바로 반환
  OCR worker     RapidOCR → 제목 + spaCy 문장 분리 (services/ocr_service.py)        [to_thread]
                 → 문장마다 MQTT 1건 (<prefix>/<session_id>/sentence, 원문 순서)
                 → 카드 전체 본문 ocr/text (대시보드 계약), 상태 <prefix>/<session_id>/status
결과 이미지/텍스트/JSON 파일은 만들지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

import cv2
import numpy as np

from services import mqtt_service, ocr_service

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_FRAMES_DIR = BASE_DIR / "tmp_frames"

BBox = Tuple[int, int, int, int]
Edge = Tuple[int, int, int]     # (x, top, bottom)

# 카드 검출: 색이 아니라 카드 좌/우 경계의 긴 세로 edge와 카드 비율(가로/세로)로 찾는다
DETECTION_WIDTH = 1280
MIN_ASPECT_RATIO = 0.45
MAX_ASPECT_RATIO = 0.70
MIN_VERTICAL_EDGE_HEIGHT_RATIO = 0.18
MIN_CARD_HEIGHT_RATIO = 0.22
MAX_VERTICAL_ANGLE_DEG = 18     # 휴대폰 촬영 시 약간의 기울기 허용
EDGE_CLUSTER_RATIO = 0.0025

# 추적 / trigger
MAX_MISSING_FRAMES = 12
FULL_VISIBILITY_MARGIN_RATIO = 0.02
TRIGGER_RATIO = float(os.getenv("OCR_TRIGGER_RATIO", "0.55"))   # 화면 왼쪽 끝 0.0 ~ 오른쪽 끝 1.0

OCR_QUEUE_SIZE = 100
SESSION_IDLE_SEC = 10 * 60      # 이 시간 동안 프레임이 없으면 세션 추적 상태를 지운다
SHUTDOWN_TIMEOUT_SEC = 30.0
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


# ---------------------------------------------------------------- 카드 검출

def box_center(bbox: BBox) -> Tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2, (y1 + y2) / 2


def box_size(bbox: BBox) -> Tuple[int, int]:
    x1, y1, x2, y2 = bbox
    return x2 - x1, y2 - y1


def cluster_vertical_edges(raw_edges: List[Edge], tolerance: int) -> List[Edge]:
    """x가 tolerance 이내로 붙은 세로 edge들을 하나로 합친다."""
    clusters: List[List[Edge]] = []
    for edge in sorted(raw_edges):
        if clusters and edge[0] - clusters[-1][-1][0] <= tolerance:
            clusters[-1].append(edge)
        else:
            clusters.append([edge])
    return [
        (int(np.median([e[0] for e in cluster])), min(e[1] for e in cluster), max(e[2] for e in cluster))
        for cluster in clusters
    ]


def find_dominant_card_band(
    edges: List[Edge], frame_height: int,
) -> Tuple[Optional[Tuple[int, int]], List[Edge]]:
    """위/아래 끝이 비슷한 세로 edge가 가장 많은 띠(카드 줄)를 찾고, 그 띠에 속한 edge만 남긴다."""
    top_tolerance = max(12, int(frame_height * 0.045))
    bottom_tolerance = max(20, int(frame_height * 0.07))
    candidates = [e for e in edges if frame_height * 0.22 <= e[2] - e[1] <= frame_height * 0.90]

    def same_band(edge: Edge, top: float, bottom: float) -> bool:
        return abs(edge[1] - top) <= top_tolerance and abs(edge[2] - bottom) <= bottom_tolerance

    best_group: List[Edge] = []
    for seed in candidates:
        group = [e for e in candidates if same_band(e, seed[1], seed[2])]
        if len(group) > len(best_group):
            best_group = group
    if len(best_group) < 2:
        return None, []

    band_top = int(np.median([e[1] for e in best_group]))
    band_bottom = int(np.median([e[2] for e in best_group]))
    filtered = sorted(e for e in candidates if same_band(e, band_top, band_bottom))
    return (band_top, band_bottom), filtered


def detect_document_cards(frame: np.ndarray, detection_width: int = DETECTION_WIDTH) -> List[BBox]:
    """프레임(BGR) → 카드 박스 목록 (원본 좌표, x1 y1 x2 y2)."""
    original_height, original_width = frame.shape[:2]
    scale = min(1.0, detection_width / original_width)
    if scale < 1.0:
        resized = cv2.resize(
            frame, (int(original_width * scale), int(original_height * scale)), interpolation=cv2.INTER_AREA,
        )
    else:
        resized = frame
    height, width = resized.shape[:2]

    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    enhanced = cv2.createCLAHE(clipLimit=1.3, tileGridSize=(8, 8)).apply(gray)   # 저대비 테마 보조
    blurred = cv2.GaussianBlur(enhanced, (9, 9), 0)
    edges = cv2.Canny(blurred, 8, 32)
    edges = cv2.morphologyEx(
        edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 9)), iterations=1,
    )
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 360,
        threshold=max(30, int(height * 0.035)),
        minLineLength=max(60, int(height * 0.16)),
        maxLineGap=max(30, int(height * 0.08)),
    )
    if lines is None:
        return []

    max_tangent = math.tan(math.radians(MAX_VERTICAL_ANGLE_DEG))
    raw_vertical_edges: List[Edge] = []
    # OpenCV 반환 shape가 (N,1,4) 또는 (N,4)여도 처리
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4).tolist():
        dx, dy = x2 - x1, y2 - y1
        if abs(dy) < height * MIN_VERTICAL_EDGE_HEIGHT_RATIO or abs(dx) > abs(dy) * max_tangent:
            continue
        raw_vertical_edges.append((int(round((x1 + x2) / 2)), min(y1, y2), max(y1, y2)))

    vertical_edges = cluster_vertical_edges(raw_vertical_edges, max(3, int(width * EDGE_CLUSTER_RATIO)))
    band, vertical_edges = find_dominant_card_band(vertical_edges, height)
    if band is None:
        return []
    band_top, band_bottom = band

    boxes: List[BBox] = []
    # 인접한 세로 edge 두 개를 카드의 좌/우 경계 후보로 사용
    for (left_x, left_top, left_bottom), (right_x, right_top, right_bottom) in zip(
        vertical_edges, vertical_edges[1:],
    ):
        card_width = right_x - left_x
        card_top = int(np.median([left_top, right_top, band_top]))
        card_bottom = int(np.median([left_bottom, right_bottom, band_bottom]))
        card_height = card_bottom - card_top
        if card_width <= 0 or card_height < max(1, height * MIN_CARD_HEIGHT_RATIO):
            continue
        if MIN_ASPECT_RATIO <= card_width / card_height <= MAX_ASPECT_RATIO:
            boxes.append((left_x, card_top, right_x, card_bottom))

    if scale < 1.0:
        # detection용 축소 좌표를 원본 프레임 좌표로 복원
        boxes = [tuple(int(round(v / scale)) for v in bbox) for bbox in boxes]
    return boxes


# ---------------------------------------------------------------- 카드 추적

@dataclass
class Track:
    track_id: int
    bbox: BBox
    center_x: float
    center_y: float
    previous_center_x: Optional[float] = None
    velocity_x: float = 0.0
    last_seen_frame: int = 0
    missing_frames: int = 0
    ever_fully_visible: bool = False
    processed: bool = False


class CardTracker:
    """카드가 가로로 흐른다는 점을 이용해 예측 x 위치에 가장 가까운 검출을 같은 track으로 잇는다."""

    def __init__(self, max_missing_frames: int = MAX_MISSING_FRAMES) -> None:
        self.tracks: Dict[int, Track] = {}
        self.next_track_id = 1
        self.max_missing_frames = max_missing_frames

    def update(self, detections: List[BBox], frame_index: int) -> Dict[int, Track]:
        possible_matches = []
        for track_id, track in self.tracks.items():
            old_width, old_height = box_size(track.bbox)
            frames_since_seen = max(1, frame_index - track.last_seen_frame)
            predicted_x = track.center_x + track.velocity_x * frames_since_seen

            for detection_index, bbox in enumerate(detections):
                new_x, new_y = box_center(bbox)
                new_width, new_height = box_size(bbox)
                max_width, max_height = max(old_width, new_width), max(old_height, new_height)
                if abs(new_y - track.center_y) > max_height * 0.25:
                    continue
                if not 0.65 < new_width / max(1, old_width) < 1.55:
                    continue
                if not 0.65 < new_height / max(1, old_height) < 1.55:
                    continue
                distance_x = abs(new_x - predicted_x)
                if distance_x > max_width * 0.75 + 20 * track.missing_frames:
                    continue
                cost = (
                    distance_x / max_width
                    + 0.4 * abs(new_width - old_width) / max_width
                    + 0.2 * abs(new_height - old_height) / max_height
                )
                possible_matches.append((cost, track_id, detection_index))

        matched_tracks, matched_detections = set(), set()
        for _, track_id, detection_index in sorted(possible_matches):     # greedy matching
            if track_id in matched_tracks or detection_index in matched_detections:
                continue
            track = self.tracks[track_id]
            bbox = detections[detection_index]
            new_x, new_y = box_center(bbox)
            delta_frames = max(1, frame_index - track.last_seen_frame)
            track.velocity_x = track.velocity_x * 0.6 + (new_x - track.center_x) / delta_frames * 0.4
            track.previous_center_x = track.center_x
            track.center_x, track.center_y = new_x, new_y
            track.bbox = bbox
            track.last_seen_frame = frame_index
            track.missing_frames = 0
            matched_tracks.add(track_id)
            matched_detections.add(detection_index)

        for track_id, track in list(self.tracks.items()):
            if track_id not in matched_tracks:
                track.missing_frames += 1
                if track.missing_frames > self.max_missing_frames:
                    del self.tracks[track_id]

        for detection_index, bbox in enumerate(detections):
            if detection_index in matched_detections:
                continue
            center_x, center_y = box_center(bbox)
            self.tracks[self.next_track_id] = Track(
                track_id=self.next_track_id, bbox=bbox,
                center_x=center_x, center_y=center_y, last_seen_frame=frame_index,
            )
            self.next_track_id += 1
        return self.tracks


def is_fully_visible(bbox: BBox, frame_width: int, frame_height: int) -> bool:
    """카드가 화면 가장자리에 걸리지 않고 전부 보이는지 (걸려 있으면 PARTIAL)."""
    x1, y1, x2, y2 = bbox
    margin_x = int(frame_width * FULL_VISIBILITY_MARGIN_RATIO)
    margin_y = max(2, int(frame_height * 0.005))
    return x1 > margin_x and x2 < frame_width - margin_x and y1 > margin_y and y2 < frame_height - margin_y


def crossed_trigger_line(previous_x: Optional[float], current_x: float, trigger_x: float) -> bool:
    """직전 프레임과 현재 프레임의 중심점이 trigger 선의 양쪽(또는 선 위)에 있으면 통과. 좌/우 방향 모두 지원."""
    if previous_x is None or previous_x == current_x:
        return False
    return (previous_x - trigger_x) * (current_x - trigger_x) <= 0


def crop_card(frame: np.ndarray, bbox: BBox) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    height, width = frame.shape[:2]
    return frame[max(0, y1):min(height, y2), max(0, x1):min(width, x2)].copy()


# ---------------------------------------------------------------- 세션 상태 / 프레임 처리

@dataclass
class CardJob:
    session_id: str
    card_index: int     # 세션에서 몇 번째로 읽는 카드인지 (1부터)
    track_id: int
    image: np.ndarray
    captured_at: float  # trigger 통과 시각 (epoch 초)


class SessionState:
    """세션 하나의 추적 상태. 다른 세션과 track/frame_index/processed를 공유하지 않는다."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.tracker = CardTracker()
        self.frame_index = 0
        self.card_count = 0
        self.last_frame_mono = time.monotonic()
        self.frame_lock = asyncio.Lock()    # 같은 세션 프레임은 하나씩, 들어온 순서대로
        self.ocr_queue: asyncio.Queue[CardJob] = asyncio.Queue(maxsize=OCR_QUEUE_SIZE)
        self.ocr_worker: Optional[asyncio.Task] = None


def process_frame(state: SessionState, jpeg_bytes: bytes) -> List[CardJob]:
    """JPEG 한 장 → 검출/추적 → 이번 프레임에서 OCR할 카드 목록. 블로킹 함수 (to_thread로 호출)."""
    frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Failed to decode JPEG")

    frame_index = state.frame_index
    state.frame_index += 1
    height, width = frame.shape[:2]
    trigger_x = width * TRIGGER_RATIO

    tracks = state.tracker.update(detect_document_cards(frame), frame_index)
    jobs: List[CardJob] = []
    for track_id, track in tracks.items():
        if track.last_seen_frame != frame_index:    # 이번 프레임에서 실제로 검출된 track만
            continue
        full_visible = is_fully_visible(track.bbox, width, height)
        if full_visible:
            track.ever_fully_visible = True
        crossed = crossed_trigger_line(track.previous_center_x, track.center_x, trigger_x)
        if not (not track.processed and track.ever_fully_visible and full_visible and crossed):
            continue

        # OCR 큐에 넣기 전에 표시해서 같은 카드가 두 번 들어가지 않도록 한다
        track.processed = True
        image = crop_card(frame, track.bbox)
        if image.size == 0:
            continue
        state.card_count += 1
        logger.info(
            "card crossed trigger (session=%s, track=%s, card=%s)", state.session_id, track_id, state.card_count,
        )
        jobs.append(CardJob(state.session_id, state.card_count, track_id, image, time.time()))
    return jobs


class OcrSink:
    """프레임 → 카드 추적 → trigger를 넘은 카드만 RapidOCR → spaCy 문장 → MQTT 발행."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionState] = {}

    async def handle(self, session_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        state = self._get_session(session_id)
        async with state.frame_lock:
            state.last_frame_mono = time.monotonic()
            jobs = await asyncio.to_thread(process_frame, state, jpeg_bytes)

        for job in jobs:
            try:
                state.ocr_queue.put_nowait(job)
            except asyncio.QueueFull:
                logger.error("card OCR queue full; card dropped (session=%s, track=%s)", session_id, job.track_id)
                self._notify(
                    session_id, "failed", message="OCR queue full",
                    track_id=job.track_id, card_index=job.card_index,
                )
        return {"frame_index": state.frame_index - 1, "cards_queued": len(jobs)}

    async def stop(self, timeout: float = SHUTDOWN_TIMEOUT_SEC) -> None:
        """대기 중인 카드 OCR/발행을 마무리하고 worker를 정리한다 (서버 종료 시)."""
        states = list(self._sessions.values())
        try:
            await asyncio.wait_for(asyncio.gather(*(s.ocr_queue.join() for s in states)), timeout)
        except asyncio.TimeoutError:
            logger.warning("card OCR queue not drained within %.0fs; remaining cards dropped", timeout)
        workers = [s.ocr_worker for s in states if s.ocr_worker is not None]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._sessions.clear()

    def _get_session(self, session_id: str) -> SessionState:
        self._prune(time.monotonic())
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState(session_id)
            state.ocr_worker = asyncio.create_task(self._ocr_worker(state))
            self._sessions[session_id] = state
            logger.info("card tracking session created (session=%s)", session_id)
        return state

    def _prune(self, now: float) -> None:
        for session_id, state in list(self._sessions.items()):
            if now - state.last_frame_mono > SESSION_IDLE_SEC and state.ocr_queue.empty() \
                    and not state.frame_lock.locked():
                if state.ocr_worker is not None:
                    state.ocr_worker.cancel()
                del self._sessions[session_id]

    async def _ocr_worker(self, state: SessionState) -> None:
        # 세션마다 하나: 카드는 trigger를 넘은 순서대로 읽고 발행한다
        while True:
            job = await state.ocr_queue.get()
            try:
                await self._read_and_publish(job)
            except Exception:
                logger.exception("card OCR worker error (session=%s, track=%s)", job.session_id, job.track_id)
            finally:
                state.ocr_queue.task_done()

    async def _read_and_publish(self, job: CardJob) -> None:
        card = {"track_id": job.track_id, "card_index": job.card_index}
        started = time.perf_counter()
        try:
            document = await asyncio.to_thread(ocr_service.read_card, job.image)
        except Exception as exc:
            logger.exception("card OCR failed (session=%s, track=%s)", job.session_id, job.track_id)
            self._notify(job.session_id, "failed", message=f"ocr failed: {exc}", **card)
            return

        title, body, sentences = document["title"], document["body"], document["sentences"]
        logger.info(
            "card OCR done (session=%s, track=%s, %.2fs, %d sentences)",
            job.session_id, job.track_id, time.perf_counter() - started, len(sentences),
        )
        if not sentences:
            self._notify(job.session_id, "skipped", title=title, message="No body sentences on card", **card)
            return

        published = publish_sentences(job, title, sentences, document["confidence"])
        # 기존 계약 유지: ocr/text → mqtt_handler → SQLite → 대시보드. 첫 줄이 문서 제목이 된다
        try:
            mqtt_service.publish_document(job.session_id, f"{title}\n{body}")
        except Exception:
            logger.exception("ocr document publish failed (session=%s)", job.session_id)
        self._notify(
            job.session_id, "ocr_completed", title=title, text=body,
            message=f"OCR completed ({published}/{len(sentences)} sentences published)",
            sentence_count=len(sentences), **card,
        )

    @staticmethod
    def _notify(
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
        # 상태 발행 실패는 OCR 처리에 영향을 주지 않는다
        try:
            mqtt_service.publish(mqtt_service.status_topic(session_id), event)
        except Exception:
            logger.exception("ocr status publish failed (session=%s)", session_id)


def publish_sentences(job: CardJob, title: str, sentences: List[str], confidence: Optional[float]) -> int:
    """문장마다 MQTT 메시지 1건을 원문 순서대로 발행한다. 실패한 문장은 로그만 남기고 다음으로 넘어간다."""
    topic = mqtt_service.sentence_topic(job.session_id)
    published = 0
    for sentence_index, sentence in enumerate(sentences):
        payload = {
            "type": "ocr_sentence",
            "message_id": f"{job.session_id}:{job.track_id}:{sentence_index}",
            "session_id": job.session_id,
            "track_id": job.track_id,
            "card_index": job.card_index,
            "title": title,
            "sentence_index": sentence_index,
            "sentence": sentence,
            "ocr_confidence": None if confidence is None else round(confidence, 4),
            "captured_at": round(job.captured_at, 3),
        }
        try:
            mqtt_service.publish(topic, payload)
            published += 1
        except Exception:
            logger.exception("ocr sentence publish failed (%s)", payload["message_id"])
    return published


_sink = OcrSink()


def get_frame_sink() -> FrameSink:
    return _sink


def startup() -> None:
    """서버 기동 시 RapidOCR/spaCy를 백그라운드 스레드에서 한 번 올린다 (기동을 막지 않음)."""
    asyncio.get_running_loop().run_in_executor(None, ocr_service.warmup)


async def shutdown() -> None:
    await _sink.stop()
