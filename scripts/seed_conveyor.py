"""컨베이어 테스트 HTML의 PASSAGES를 session_id='seed'로 ocr_documents에 저장한다.
실제 OCR 없이 RAG 검색(/api/search)을 시험하기 위한 데이터.

사용법 (저장소 루트에서):
    python -m scripts.seed_conveyor C:\\Users\\1\\Downloads\\ocr-conveyor-test.html
    python -m scripts.seed_conveyor <html> --dry-run     # 파싱 결과만 출력
    python -m scripts.seed_conveyor <html> --limit 10    # 앞 10개만

동작: 모든 문단을 먼저 임베딩한 뒤, 기존 session_id='seed' 행을 지우고 새로 저장한다
(임베딩 도중 실패하면 기존 seed 행은 그대로 남음). 환경변수는 .env에서 읽는다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import List, Tuple

from dotenv import load_dotenv

SEED_SESSION_ID = "seed"
# 무료 등급 임베딩 한도에 걸리면 잠시 쉬었다가 다시 시도
RATE_LIMIT_WAIT_SECONDS = 30
RATE_LIMIT_RETRIES = 4

# const PASSAGES = [ { title: "...", body: [ "...", ... ]}, ... ];
_PASSAGES_RE = re.compile(r"const PASSAGES\s*=\s*\[(.*?)\n\];", re.S)
_ITEM_RE = re.compile(r'title:\s*("(?:[^"\\]|\\.)*")\s*,\s*body:\s*\[(.*?)\]\s*\}', re.S)
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def parse_passages(html: str) -> List[Tuple[str, str]]:
    """(title, text) 목록. text는 body 문단들을 줄바꿈으로 이은 것."""
    match = _PASSAGES_RE.search(html)
    if not match:
        raise ValueError("const PASSAGES = [...] not found")
    passages = []
    for title, body in _ITEM_RE.findall(match.group(1)):
        # JS 큰따옴표 문자열 = JSON 문자열 (\" 이스케이프 처리)
        paragraphs = [json.loads(s) for s in _STRING_RE.findall(body)]
        passages.append((json.loads(title), "\n".join(paragraphs)))
    return passages


def embed_with_retry(text: str) -> List[float]:
    from services import gemini_service

    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return gemini_service.embed_text(text)
        except Exception as exc:
            if not gemini_service.is_rate_limit_error(exc) or attempt == RATE_LIMIT_RETRIES:
                raise
            print(f"  rate limited, waiting {RATE_LIMIT_WAIT_SECONDS}s ...", flush=True)
            time.sleep(RATE_LIMIT_WAIT_SECONDS)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("html", type=Path, help="ocr-conveyor-test.html path")
    parser.add_argument("--dry-run", action="store_true", help="parse only, no API calls")
    parser.add_argument("--limit", type=int, default=None, help="seed only the first N passages")
    args = parser.parse_args()

    passages = parse_passages(args.html.read_text(encoding="utf-8"))[: args.limit]
    print(f"parsed {len(passages)} passages")
    if args.dry_run:
        for i, (title, text) in enumerate(passages, 1):
            print(f"{i:3d}. {title} ({len(text)} chars)")
        return 0

    load_dotenv(".env")
    from db import supabase

    embeddings = []
    for i, (title, text) in enumerate(passages, 1):
        embeddings.append(embed_with_retry(text))
        print(f"embedded {i}/{len(passages)}: {title}", flush=True)

    deleted = (
        supabase.get_client()
        .table(supabase.OCR_TABLE)
        .delete()
        .eq("session_id", SEED_SESSION_ID)
        .execute()
    )
    print(f"deleted {len(deleted.data or [])} existing seed rows")

    for (title, text), embedding in zip(passages, embeddings):
        supabase.save_ocr_document(SEED_SESSION_ID, title, text, embedding)
    print(f"inserted {len(passages)} rows with session_id='{SEED_SESSION_ID}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
