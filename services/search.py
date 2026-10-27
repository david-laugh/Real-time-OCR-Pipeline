"""
============================================================
[FILE] services/search.py
[역할] 텍스트 검색 서비스 — SQLite FTS5 기반 전문검색
============================================================
"""

from db.meta_store import MetaStore


class SearchService:
    def __init__(self, meta_store: MetaStore):
        self._store = meta_store

    def _build_fts_query(self, query: str) -> str:
        words = query.strip().split()
        if not words:
            return ""
        sanitized = []
        for w in words:
            cleaned = "".join(c for c in w if c.isalnum() or c in "-_")
            if cleaned:
                sanitized.append('"' + cleaned + '"')
        if not sanitized:
            return ""
        return " OR ".join(sanitized)

    def search(self, query: str, limit: int = 5) -> dict:
        query = query.strip()
        if not query:
            return {"answer": "", "sources": []}

        fts_query = self._build_fts_query(query)
        if not fts_query:
            return {"answer": "", "sources": []}

        results = self._store.search(fts_query, limit=limit)

        if not results:
            return {"answer": "저장된 문서에서 관련 내용을 찾을 수 없습니다.", "sources": []}

        top = results[0]
        sources = [
            {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "preview": r["content"][:120] + ("..." if len(r["content"]) > 120 else ""),
                "score": r["score"],
            }
            for r in results
        ]

        return {
            "answer": top["content"],
            "sources": sources,
        }
