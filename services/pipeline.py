"""
============================================================
[FILE] services/pipeline.py
[역할] 텍스트 처리 오케스트레이터 — dedup → 저장 → broadcast
[입력] OCR 처리 완료된 텍스트 문자열
[합칠 때] 벡터 임베딩 단계 삽입 (process() 내 step 3 위치)
         카메라 프레임 → OCR 단계를 앞에 연결
============================================================
"""

from datetime import datetime, timezone
from services.dedup import DedupFilter
from db.meta_store import MetaStore


class Pipeline:
    def __init__(self, meta_store: MetaStore, dedup: DedupFilter):
        self._store = meta_store
        self._dedup = dedup
        self._counter = meta_store.get_count()

    def process(self, text: str, task_id: str = "default") -> dict | None:
        text = text.strip()
        if self._dedup.is_duplicate(text):
            return None

        self._counter += 1
        doc_id = f"doc_{self._counter:03d}"

        lines = text.split("\n")
        title = lines[0][:60].strip() if lines else text[:60]

        now = datetime.now(timezone.utc).isoformat()
        self._store.save(doc_id, title, text, task_id=task_id)

        return {
            "doc_id": doc_id,
            "task_id": task_id,
            "title": title,
            "char_count": len(text),
            "timestamp": now,
        }
