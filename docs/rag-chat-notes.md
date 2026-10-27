# RAG 챗봇 작업 노트 (담당: 1번 (대시보드 연동은 3번))

> 새 세션에서 이 문서만 읽고 이어서 작업할 수 있도록 유지한다.
> 카메라/QR 관련 기록은 `docs/mobile-camera-notes.md`, `docs/qr-pairing-notes.md`.
> 마지막 업데이트: 2026-10-27

## 목표
Supabase `ocr_documents`에 쌓인 OCR 텍스트를 근거로 질문에 답하는 챗봇.
대시보드 전체는 다른 팀원 담당일 수 있으므로 챗봇은 **독립 조각(`<rag-chat>`)**으로 만들고, 단독 테스트 페이지 `/chat`을 둔다.

## 현재 상태 (브랜치 `feature/rag-chat`, 기준 `origin/release` 17a9725 = 카메라+OCR+QR)
- [x] 브랜치 재생성 (이전 브랜치는 pec 강제 병합 상태 31bc5e0 기준이라 삭제 후 17a9725에서 새로 만듦)
- [x] pec 파일 확인: `api/routes.py`, `.env.example` 없음 / `main.py`에 `pair`, `session` 라우터 있음
  - `.claude/launch.json`: release에 남은 pec 잔여 파일, 이 브랜치에서는 건드리지 않음
- [x] 노트 작성 (1단계)
- [x] RPC SQL 파일 `db/sql/match_ocr_documents.sql`
- [x] Supabase SQL Editor에서 위 파일 실행 (2026-10-27, "Success. No rows returned")
- [ ] **서버: `gemini_service.py`/`db/supabase.py` 최소 수정** ← 다음 할 일, `services/rag_service.py`, `api/search.py`, `main.py`
- [ ] 시드 스크립트 `scripts/seed_conveyor.py` → `session_id='seed'`로 컨베이어 문단 저장
- [ ] `<rag-chat>` 조각 `static/chat/rag-chat.js`
- [ ] `/chat` 단독 페이지 `static/chat/index.html`
- [ ] 로컬 확인 → push, PR(→ release)

## 흐름
```
질문 ──► embed (task_type=RETRIEVAL_QUERY, 768차원)
     ──► Supabase RPC match_ocr_documents(query_embedding, match_count, filter_session)
     ──► 상위 N개 문서를 근거로 Gemini 답변 생성
     ──► {answer, sources}
```

## 결정사항
| 항목 | 결정 | 이유 |
|---|---|---|
| API | `POST /api/search` | pec 대시보드가 쓰던 경로와 같게 |
| 요청 | `{query, limit=5, session_id?}` | `session_id` 없으면 전체 문서 검색 |
| 응답 | `{answer, sources:[{doc_id, title, preview, score}]}` | pec 형식 그대로. `doc_id`=`ocr_documents.id`(uuid), `preview`=본문 앞 120자(+`...`), `score`=코사인 유사도(0~1) |
| 빈 질문 | `{answer:"", sources:[]}` | pec 동작과 같게 |
| 검색 결과 없음 | `{answer:"저장된 문서에서 관련 내용을 찾을 수 없습니다.", sources:[]}`, Gemini 호출 안 함 | pec 문구 그대로, 비용 절약 |
| 질문 임베딩 | 문서 저장과 같은 모델·차원, task_type만 `RETRIEVAL_QUERY` | 문서는 `RETRIEVAL_DOCUMENT`로 저장됨 |
| 유사도 검색 | Supabase RPC(pgvector `<=>`) | supabase-py에서 벡터 연산은 RPC로만 가능 |
| 답변 모델 | 환경변수 `GEMINI_CHAT_MODEL`(기본값은 구현 시 사용 가능한 Flash 계열로 확인) | |
| 답변 규칙 | 근거 문서 안에서만 답하고, 없으면 모른다고 답함. 질문 언어로 답함(문서는 영어) | |
| 2번 파일 수정 | `gemini_service.py`: `embed_text`에 `task_type` 인자(기본값 그대로) + 답변 생성 함수 1개 / `db/supabase.py`: RPC 호출 함수 1개 | 최소 수정, 기존 호출부 영향 없음 |
| 시드 | 컨베이어 HTML의 `PASSAGES`(100개, `{title, body[]}`)를 `session_id='seed'`로 저장 | 실제 OCR 없이 검색 테스트 |
| 시드 재실행 | 저장 전에 `session_id='seed'` 행을 지움 | 중복 방지 |
| 형태 | 커스텀 태그 `<rag-chat>` + Shadow DOM, 스크립트 1개 | `<qr-pairing>`과 같은 방식 |
| main.py | `3번 (대시보드/RAG):` 주석 아래 라우터 등록 + import 줄에 추가 | |
| CORS | 수정 안 함 | `/chat`은 같은 origin. github.io 대시보드에서 부를 경우 지금 설정(POST 허용)으로 가능 |

## Supabase RPC (`db/sql/match_ocr_documents.sql`, SQL Editor에서 1회 실행)
```sql
create or replace function match_ocr_documents(
    query_embedding vector(768),
    match_count     int  default 5,
    filter_session  text default null
)
returns table (id uuid, session_id text, title text, text text, similarity float)
language sql stable
as $$
    select d.id, d.session_id, d.title, d.text,
           1 - (d.embedding <=> query_embedding) as similarity
    from ocr_documents d
    where filter_session is null or d.session_id = filter_session
    order by d.embedding <=> query_embedding
    limit match_count;
$$;
```
- 차원 768 = `GEMINI_EMBEDDING_DIM`. 바꾸면 테이블과 함수 둘 다 바꿔야 함
- 문서 수가 적어 인덱스 없이 시작. 많아지면 `hnsw (embedding vector_cosine_ops)` 인덱스 추가

## 환경변수 (`.env`, 커밋 안 함)
| 키 | 필수 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | O | |
| `SUPABASE_URL` | O | |
| `SUPABASE_KEY` | O | 서버 전용 키(service_role). 브라우저로 내보내지 않음 |
| `GEMINI_CHAT_MODEL` | | 답변 모델 (새로 추가) |
| `GEMINI_EMBEDDING_MODEL` | | 기본 `gemini-embedding-001` |
| `GEMINI_EMBEDDING_DIM` | | 기본 768 |
| `SUPABASE_OCR_TABLE` | | 기본 `ocr_documents` |
- 코드에서 `.env`를 직접 읽지 않음 → `uvicorn main:app --env-file .env`로 실행 (`uvicorn[standard]`에 python-dotenv 포함)

## 파일 (예정)
```
services/gemini_service.py   embed_text(task_type=...), generate_answer() 추가
db/supabase.py               match_ocr_documents() RPC 호출 추가
db/sql/match_ocr_documents.sql  RPC 정의 (SQL Editor에서 실행)
services/rag_service.py      embed → match → answer, pec 응답 형식으로 변환
api/search.py                POST /api/search, GET /chat
main.py                      라우터 등록
static/chat/rag-chat.js      <rag-chat> 조각
static/chat/index.html       /chat 테스트 페이지
scripts/seed_conveyor.py     컨베이어 HTML 경로를 인자로 받아 seed 저장
```

## 시드 원본
- `C:\Users\1\Downloads\ocr-conveyor-test.html` (저장소에 넣지 않음, 경로를 인자로 전달)
- `PASSAGES` 100개, 영어 문단. title → `title`, body 문단들을 줄바꿈으로 이어 → `text`

## 테스트 기록
- (아직 없음)

## 남은 일 / 이슈
- `.claude/launch.json`: release에 남은 pec 잔여 파일, 이 브랜치에서는 건드리지 않음
- 대시보드 담당자에게 `<rag-chat>` 붙이는 방법 공유
