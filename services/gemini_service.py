"""Gemini API 호출 (google-genai SDK). Gemini SDK는 이 파일에서만 import 한다.
embedding과 RAG 답변 생성에 사용한다 (OCR은 services/ocr_service.py의 EasyOCR).

환경변수
  GEMINI_API_KEY          (필수)
  GEMINI_EMBEDDING_MODEL  기본 gemini-embedding-001
  GEMINI_EMBEDDING_DIM    기본 768 (Supabase vector(N) 컬럼 차원과 같아야 함)
  GEMINI_CHAT_MODEL           기본 gemini-3.5-flash-lite (RAG 답변)
  GEMINI_CHAT_FALLBACK_MODEL  기본 gemini-3.5-flash (사용량 한도 429 시 1회 재시도, 빈 값이면 재시도 안 함)

SDK가 동기식이므로 async 코드에서는 asyncio.to_thread()로 호출할 것.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List, Optional

from google import genai
from google.genai import errors, types

EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.getenv("GEMINI_EMBEDDING_DIM", "768"))
CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.5-flash-lite")
CHAT_FALLBACK_MODEL = os.getenv("GEMINI_CHAT_FALLBACK_MODEL", "gemini-3.5-flash")


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


def generate_text(
    prompt: str,
    system_instruction: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    response = get_client().models.generate_content(
        model=model or CHAT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=system_instruction),
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty answer")
    return response.text.strip()


def is_rate_limit_error(exc: BaseException) -> bool:
    """사용량 한도 초과(429 RESOURCE_EXHAUSTED) 여부."""
    return isinstance(exc, errors.APIError) and exc.code == 429
