"""OCR 텍스트 → embedding vector."""

from __future__ import annotations

import asyncio
from typing import List

from services import gemini_service

EMBEDDING_DIM = gemini_service.EMBEDDING_DIM


async def create_embedding(text: str) -> List[float]:
    text = text.strip()
    if not text:
        raise ValueError("cannot embed empty text")
    # google-genai 동기 호출 → 이벤트 루프를 막지 않도록 스레드로
    return await asyncio.to_thread(gemini_service.embed_text, text)
