"""JPEG bytes → 영어 원문 추출 (EasyOCR, 로컬 실행).

extract_text()는 블로킹 함수다. async 코드에서는 asyncio.to_thread()로 호출할 것.
글자가 없는 프레임이면 {"title": "", "text": ""}를 돌려준다(예외 아님).

EasyOCR은 글자 박스를 읽는 순서대로 주지 않으므로, y좌표로 줄을 묶고
줄 안에서는 x좌표 순으로 이어 붙인다. 인식된 글자 자체는 고치지 않는다.
첫 실행 때 모델 파일(~100MB)을 ~/.EasyOCR/ 에 내려받는다.
"""

from __future__ import annotations

import threading
from statistics import median
from typing import Dict, List, Optional

import easyocr

MIN_CONFIDENCE = 0.3        # 이보다 낮은 박스는 버린다 (잡음/오인식)
TITLE_HEIGHT_RATIO = 1.3    # 첫 줄 글자 높이가 본문의 이 배수 이상이면 제목
PARAGRAPH_GAP_RATIO = 0.8   # 줄 간격이 글자 높이의 이 배수보다 크면 문단 구분
TITLE_MAX_CHARS = 120

_reader: Optional[easyocr.Reader] = None
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


def extract_text(jpeg_bytes: bytes) -> Dict[str, str]:
    with _lock:
        results = _get_reader().readtext(jpeg_bytes)

    lines = _group_lines(results)
    if not lines:
        return {"title": "", "text": ""}

    title = lines[0].text
    body = lines
    if len(lines) > 1:
        body_height = median(line.height for line in lines[1:])
        if lines[0].height >= body_height * TITLE_HEIGHT_RATIO:
            body = lines[1:]  # 큰 글씨 첫 줄 = 제목, 본문에서 제외
    return {"title": title[:TITLE_MAX_CHARS].strip(), "text": _join_lines(body)}


def _get_reader() -> easyocr.Reader:
    # import 시점이 아니라 첫 프레임 때 모델을 올린다 (서버 기동을 막지 않도록)
    global _reader
    if _reader is None:
        _reader = easyocr.Reader(["en"], verbose=False)
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
