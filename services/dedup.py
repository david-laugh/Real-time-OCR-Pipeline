"""
============================================================
[FILE] services/dedup.py
[역할] 중복 텍스트 필터링 — 유사도 기반 + 최소 길이 체크
[합칠 때] OCR 결과의 confidence 값 기반 필터 추가 가능
============================================================
"""

from collections import deque
from difflib import SequenceMatcher


class DedupFilter:
    def __init__(self, threshold: float = 0.85, buffer_size: int = 5, min_length: int = 20):
        self._threshold = threshold
        self._min_length = min_length
        self._buffer: deque[str] = deque(maxlen=buffer_size)

    def is_duplicate(self, text: str) -> bool:
        if len(text.strip()) < self._min_length:
            return True
        for prev in self._buffer:
            ratio = SequenceMatcher(None, prev, text).ratio()
            if ratio > self._threshold:
                return True
        self._buffer.append(text)
        return False

    def reset(self):
        self._buffer.clear()
