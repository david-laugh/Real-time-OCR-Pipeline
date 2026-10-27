"""JPEG bytes → 흰색 문서 카드 검출/crop → 영어 원문 추출 (RapidOCR, 로컬 실행).

find_cards() / crop_card() / extract_text()는 블로킹 함수다. async 코드에서는
asyncio.to_thread()로 호출할 것.
글자가 없는 이미지면 extract_text()는 {"title": "", "text": ""}를 돌려준다(예외 아님).

카드 검출: 어두운 배경 위의 큰 흰색 세로형 직사각형을
grayscale → threshold → morphology(open) → findContours → boundingRect 로 찾는다.
어느 카드를 언제 OCR할지는 frame_sink.OcrSink가 정한다.

RapidOCR도 글자 박스를 읽는 순서대로 주지 않으므로, y좌표로 줄을 묶고
줄 안에서는 x좌표 순으로 이어 붙인다. 인식된 글자 자체는 고치지 않는다.
모델(ONNX)은 rapidocr_onnxruntime 패키지에 포함되어 있다.
"""

from __future__ import annotations

import threading
from statistics import median
from typing import Dict, List, Optional, Tuple, Union

import cv2
import numpy as np


MIN_CONFIDENCE = 0.3        # 이보다 낮은 박스는 버린다 (잡음/오인식)
TITLE_HEIGHT_RATIO = 1.3    # 첫 줄 글자 높이가 본문의 이 배수 이상이면 제목
PARAGRAPH_GAP_RATIO = 0.8   # 줄 간격이 글자 높이의 이 배수보다 크면 문단 구분
TITLE_MAX_CHARS = 120

# --- 흰색 카드 검출 (값은 모두 프레임 크기 대비 비율) ---
WHITE_THRESHOLD = 160         # grayscale에서 이 값 이상이면 흰색 (모니터 촬영이라 255보다 한참 낮게)
MORPH_KERNEL_SIZE = 5         # open 연산 커널(px): 작은 흰 점/잡음 제거
MIN_CARD_WIDTH_RATIO = 0.05   # 카드 폭 >= 프레임 폭 × 이 값
MIN_CARD_HEIGHT_RATIO = 0.25  # 카드 높이 >= 프레임 높이 × 이 값
MIN_CARD_AREA_RATIO = 0.02    # 카드 면적 >= 프레임 면적 × 이 값
MIN_CARD_ASPECT = 1.1         # 높이/폭 >= 이 값 (세로형, 9:16이면 약 1.78)
MIN_CARD_FILL_RATIO = 0.75    # 윤곽 면적/박스 면적 >= 이 값 (직사각형다운 덩어리만)

# --- OCR 직전 crop 후처리 ---
CROP_PADDING_RATIO = 0.01     # 박스 바깥으로 카드 크기의 이 비율만큼 여유
CROP_MIN_HEIGHT = 960         # crop 높이가 이보다 작으면 이 높이로 확대 (작은 글씨 대비)

Box = Tuple[int, int, int, int]  # x, y, w, h

_reader: Optional["RapidOCR"] = None
# Reader 생성과 추론을 한 번에 하나씩 (여러 세션이 동시에 와도 CPU를 나눠 쓰지 않도록)
_lock = threading.Lock()


class _Line:
    def __init__(self, top: float, bottom: float, left: float, text: str) -> None:
        self.top, self.bottom = top, bottom
        self.words = [(left, text)]
        self.heights = [bottom - top]

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return median(self.heights)

    @property
    def text(self) -> str:
        return " ".join(text for _, text in sorted(self.words))

    def add(self, top: float, bottom: float, left: float, text: str) -> None:
        self.top, self.bottom = min(self.top, top), max(self.bottom, bottom)
        self.words.append((left, text))
        self.heights.append(bottom - top)


def decode_frame(jpeg_bytes: bytes) -> Optional[np.ndarray]:
    """JPEG → BGR 이미지. 깨진 데이터면 None."""
    data = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def detect_white_cards(frame: np.ndarray) -> List[Box]:
    """프레임에서 흰색 문서 카드 후보의 (x, y, w, h)를 왼쪽부터 돌려준다.

    화면 가장자리에 잘린 카드도 포함한다(추적은 해야 하므로).
    잘린 카드를 OCR하지 않는 판단은 frame_sink에서 한다.
    """
    frame_h, frame_w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(gray, WHITE_THRESHOLD, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    # RETR_EXTERNAL: 카드 안의 글자 구멍은 무시하고 바깥 윤곽만
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    cards: List[Box] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < frame_w * MIN_CARD_WIDTH_RATIO or h < frame_h * MIN_CARD_HEIGHT_RATIO:
            continue
        if w * h < frame_w * frame_h * MIN_CARD_AREA_RATIO:
            continue
        if h < w * MIN_CARD_ASPECT:
            continue
        if cv2.contourArea(contour) < w * h * MIN_CARD_FILL_RATIO:
            continue
        cards.append((x, y, w, h))
    return sorted(cards)


def find_cards(jpeg_bytes: bytes) -> Tuple[Optional[np.ndarray], List[Box]]:
    """decode + detect를 한 번에 (스레드 한 번으로 넘기기 위해)."""
    frame = decode_frame(jpeg_bytes)
    if frame is None:
        return None, []
    return frame, detect_white_cards(frame)


def crop_card(frame: np.ndarray, box: Box) -> np.ndarray:
    """카드 영역만 잘라 OCR용으로 돌려준다. 색은 그대로 두고 작으면 확대만 한다."""
    frame_h, frame_w = frame.shape[:2]
    x, y, w, h = box
    pad_x, pad_y = int(w * CROP_PADDING_RATIO), int(h * CROP_PADDING_RATIO)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(frame_w, x + w + pad_x), min(frame_h, y + h + pad_y)
    crop = frame[y1:y2, x1:x2]
    if 0 < crop.shape[0] < CROP_MIN_HEIGHT:
        scale = CROP_MIN_HEIGHT / crop.shape[0]
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return np.ascontiguousarray(crop)


def extract_text(image: Union[bytes, np.ndarray]) -> Dict[str, str]:
    """JPEG bytes 또는 BGR 이미지(crop_card 결과)에서 제목/본문을 읽는다."""
    with _lock:
        results, _ = _get_reader()(image)

    lines = _group_lines(results or [])
    if not lines:
        return {"title": "", "text": ""}

    title = lines[0].text
    body = lines
    if len(lines) > 1:
        body_height = median(line.height for line in lines[1:])
        if lines[0].height >= body_height * TITLE_HEIGHT_RATIO:
            body = lines[1:]  # 큰 글씨 첫 줄 = 제목, 본문에서 제외
    return {"title": title[:TITLE_MAX_CHARS].strip(), "text": _join_lines(body)}


def _get_reader() -> "RapidOCR":
    # import 시점이 아니라 첫 OCR 때 모델을 올린다 (서버 기동을 막지 않도록)
    global _reader
    if _reader is None:
        from rapidocr_onnxruntime import RapidOCR

        _reader = RapidOCR()
    return _reader


def _group_lines(results: list) -> List[_Line]:
    boxes = []
    for box, text, confidence in results:
        text = text.strip()
        if not text or confidence < MIN_CONFIDENCE:
            continue
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        boxes.append((min(ys), max(ys), min(xs), text))

    lines: List[_Line] = []
    for top, bottom, left, text in sorted(boxes, key=lambda b: (b[0] + b[1]) / 2):
        center = (top + bottom) / 2
        if lines and lines[-1].top <= center <= lines[-1].bottom:
            lines[-1].add(top, bottom, left, text)
        else:
            lines.append(_Line(top, bottom, left, text))
    return lines


def _join_lines(lines: List[_Line]) -> str:
    if not lines:
        return ""
    gap_limit = median(line.height for line in lines) * PARAGRAPH_GAP_RATIO
    parts = [lines[0].text]
    for prev, line in zip(lines, lines[1:]):
        parts.append("\n\n" if line.top - prev.bottom > gap_limit else "\n")
        parts.append(line.text)
    return "".join(parts)
