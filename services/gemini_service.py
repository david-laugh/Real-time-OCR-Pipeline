"""Gemini API 호출 (google-genai SDK). Gemini SDK는 이 파일에서만 import 한다.
embedding과 RAG 답변 생성에 사용한다 (OCR은 services/ocr_service.py의 EasyOCR).

환경변수
  GEMINI_API_KEY          (필수)
  GEMINI_EMBEDDING_MODEL  기본 gemini-embedding-001
  GEMINI_EMBEDDING_DIM    기본 768 (Supabase vector(N) 컬럼 차원과 같아야 함)
  GEMINI_CHAT_MODEL       기본 gemini-2.5-flash (RAG 답변)

SDK가 동기식이므로 async 코드에서는 asyncio.to_thread()로 호출할 것.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional

from google import genai
from google.genai import types

EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.getenv("GEMINI_EMBEDDING_DIM", "768"))
CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    # import 시점이 아니라 첫 호출 때 만든다 (키 없이도 서버는 뜨도록)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def embed_text(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> List[float]:
    # 문서 저장은 RETRIEVAL_DOCUMENT, 검색 질문은 RETRIEVAL_QUERY
    response = get_client().models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBEDDING_DIM,
        ),
    )
    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError("Gemini returned an empty embedding")
    return list(response.embeddings[0].values)


def generate_text(prompt: str, system_instruction: Optional[str] = None) -> str:
    response = get_client().models.generate_content(
        model=CHAT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty answer")
    return response.text.strip()
