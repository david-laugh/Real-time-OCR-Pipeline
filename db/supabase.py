"""Supabase 접근. Supabase SDK는 이 파일에서만 import 한다.

환경변수
  SUPABASE_URL, SUPABASE_KEY  (필수, 서버 전용 키)
  SUPABASE_OCR_TABLE          기본 ocr_documents

예상 테이블 (Supabase SQL Editor에서 실행, vector 차원 = GEMINI_EMBEDDING_DIM):

    create extension if not exists vector;

    create table if not exists ocr_documents (
        id          uuid primary key default gen_random_uuid(),
        session_id  text not null,
        title       text not null default '',
        text        text not null,
        embedding   vector(768) not null,
        created_at  timestamptz not null default now()
    );
    create index if not exists ocr_documents_session_idx on ocr_documents (session_id);

SDK가 동기식이므로 async 코드에서는 asyncio.to_thread()로 호출할 것.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, List

from supabase import Client, create_client

OCR_TABLE = os.getenv("SUPABASE_OCR_TABLE", "ocr_documents")


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY is not set")
    return create_client(url, key)


def save_ocr_document(
    session_id: str,
    title: str,
    text: str,
    embedding: List[float],
) -> Dict[str, Any]:
    """한 행을 저장하고 저장된 행(id, created_at 포함)을 돌려준다."""
    response = (
        get_client()
        .table(OCR_TABLE)
        .insert(
            {
                "session_id": session_id,
                "title": title,
                "text": text,
                "embedding": embedding,
            }
        )
        .execute()
    )
    if not response.data:
        raise RuntimeError(f"Supabase insert into {OCR_TABLE} returned no row")
    return response.data[0]
