"""Gemini API 호출 (google-genai SDK). Gemini SDK는 이 파일에서만 import 한다.
현재는 embedding에만 사용한다 (OCR은 services/ocr_service.py의 EasyOCR).

환경변수
  GEMINI_API_KEY          (필수)
  GEMINI_EMBEDDING_MODEL  기본 gemini-embedding-001
  GEMINI_EMBEDDING_DIM    기본 768 (Supabase vector(N) 컬럼 차원과 같아야 함)

SDK가 동기식이므로 async 코드에서는 asyncio.to_thread()로 호출할 것.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from google import genai
from google.genai import types

EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.getenv("GEMINI_EMBEDDING_DIM", "768"))


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    # import 시점이 아니라 첫 호출 때 만든다 (키 없이도 서버는 뜨도록)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return genai.Client(api_key=api_key)


def embed_text(text: str) -> List[float]:
    response = get_client().models.embed_content(
        model=EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=EMBEDDING_DIM,
        ),
    )
    if not response.embeddings or not response.embeddings[0].values:
        raise RuntimeError("Gemini returned an empty embedding")
    return list(response.embeddings[0].values)
