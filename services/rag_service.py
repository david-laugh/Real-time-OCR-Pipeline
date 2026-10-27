"""RAG 검색: 질문 → embedding(RETRIEVAL_QUERY) → Supabase RPC → Gemini 답변.

응답 형식은 pec 대시보드와 같다:
    {"answer": str, "sources": [{"doc_id", "title", "preview", "score"}]}

같은 카드가 OCR 오인식으로 제목만 다르게 여러 번 저장될 수 있으므로
(예: "READING IN TRANSLATION" / "{G IN TRANSLATION") 검색 결과끼리 본문이
매우 비슷하면 본문이 더 긴 쪽 하나만 남긴다. OCR 본문은 줄 순서가 섞이고
오타가 많아 순서 기반(SequenceMatcher)이 아니라 단어 집합 Jaccard로 비교한다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set

from db import supabase
from services import gemini_service

logger = logging.getLogger(__name__)

PREVIEW_CHARS = 120
TITLE_FALLBACK_CHARS = 40
# 같은 카드로 볼 단어 집합 Jaccard. 실데이터(2026-10-27): 같은 카드 0.47~0.68,
# 다른 카드/두 카드에 걸친 조각 0.43 이하
DUPLICATE_JACCARD = 0.5
# 중복 제거 후에도 limit개를 채울 수 있도록 넉넉히 가져온다
CANDIDATE_FACTOR = 3
MAX_CONTEXT_CHARS = 2000

NO_RESULT_ANSWER = "저장된 문서에서 관련 내용을 찾을 수 없습니다."

SYSTEM_INSTRUCTION = (
    "You answer questions using only the numbered documents provided. "
    "The documents are OCR text and may contain recognition errors. "
    "If the documents do not contain the answer, say that you could not find it in the saved documents. "
    "Answer in the same language as the question, concisely, "
    "and cite the documents you used like [1], [2]."
)

_LETTER_RE = re.compile(r"[^\W\d_]")
_WORD_RE = re.compile(r"[a-z]{3,}")


async def search(query: str, limit: int = 5, session_id: Optional[str] = None) -> Dict[str, Any]:
    query = query.strip()
    if not query:
        return {"answer": "", "sources": []}

    # google-genai / supabase 동기 호출 → 이벤트 루프를 막지 않도록 스레드로
    embedding = await asyncio.to_thread(gemini_service.embed_text, query, "RETRIEVAL_QUERY")
    rows = await asyncio.to_thread(
        supabase.match_ocr_documents, embedding, limit * CANDIDATE_FACTOR, session_id
    )
    docs = dedupe_by_text(rows)[:limit]
    if not docs:
        return {"answer": NO_RESULT_ANSWER, "sources": []}

    prompt = build_prompt(query, docs)
    answer = await asyncio.to_thread(gemini_service.generate_text, prompt, SYSTEM_INSTRUCTION)
    return {"answer": answer, "sources": [to_source(d) for d in docs]}


def dedupe_by_text(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """유사도 순서를 유지하며 본문이 거의 같은 행을 하나로 합친다.
    그룹의 아무 행과 비슷하면 같은 그룹이다. 그룹마다 본문이 가장 긴 행을 남기고,
    score는 그룹에서 가장 높은 값을 쓴다."""
    groups: List[Dict[str, Any]] = []  # {"best": row, "score": float, "words": [set, ...]}
    for row in rows:
        words = _words(row.get("text") or "")
        score = float(row.get("similarity") or 0)
        group = next((g for g in groups if any(_is_same_text(words, w) for w in g["words"])), None)
        if group is None:
            groups.append({"best": row, "score": score, "words": [words]})
            continue
        group["words"].append(words)
        group["score"] = max(group["score"], score)
        if len(row.get("text") or "") > len(group["best"].get("text") or ""):
            group["best"] = row
    return [{**g["best"], "similarity": g["score"]} for g in groups]


def display_title(title: str, text: str) -> str:
    """제목이 비었거나 글자 없이 숫자/기호뿐이면(예: 카드 번호 "214") 본문 첫 줄 앞부분을 쓴다.
    본문도 카드 번호 등으로 시작할 수 있으므로 글자 위주인 첫 줄을 고른다."""
    title = (title or "").strip()
    if _LETTER_RE.search(title):
        return title
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    first_line = next((line for line in lines if _is_wordy(line)), lines[0] if lines else "")
    return _truncate(first_line, TITLE_FALLBACK_CHARS) or title


def to_source(row: Dict[str, Any]) -> Dict[str, Any]:
    text = row.get("text") or ""
    return {
        "doc_id": row["id"],
        "title": display_title(row.get("title") or "", text),
        "preview": _truncate(text, PREVIEW_CHARS),
        "score": round(float(row.get("similarity") or 0), 4),
    }


def build_prompt(query: str, docs: List[Dict[str, Any]]) -> str:
    parts = []
    for i, d in enumerate(docs, 1):
        title = display_title(d.get("title") or "", d.get("text") or "")
        parts.append(f"[{i}] {title}\n{_truncate(d.get('text') or '', MAX_CONTEXT_CHARS)}")
    return "Documents:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {query}"


def _is_same_text(a: Set[str], b: Set[str]) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= DUPLICATE_JACCARD


def _words(text: str) -> Set[str]:
    # 3글자 이상 영문 단어만 (숫자·짧은 OCR 조각 제외)
    return set(_WORD_RE.findall(text.lower()))


def _is_wordy(line: str) -> bool:
    """글자가 4개 이상이고 공백 외 문자의 60% 이상이 글자인 줄."""
    letters = len(_LETTER_RE.findall(line))
    visible = len(line.replace(" ", ""))
    return letters >= 4 and letters >= visible * 0.6


def _truncate(text: str, limit: int) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")
