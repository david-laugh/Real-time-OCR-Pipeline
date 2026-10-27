# Real-time OCR Pipeline

휴대폰 카메라로 찍은 영어 문서를 OCR → embedding → Supabase에 저장하고, 진행 상태를 MQTT로 대시보드에 보낸다.

```
휴대폰 카메라 페이지 ──POST /frame (JPEG 1장)──▶ FastAPI ── 즉시 202
                                                   │ background (frame_queue)
                                                   ▼
                                     frame_sink.OcrSink.handle()
                         EasyOCR → Gemini embedding → Supabase 저장
                                                   │
                                     MQTT  ocr/<session_id>/status ──▶ 대시보드
```

## 1. 준비

**Python 3.10** (팀 공통 버전)

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt     # easyocr가 torch를 함께 설치 (1~2GB)
```

첫 프레임 처리 때 EasyOCR 모델(~100MB)을 `~/.EasyOCR/`에 내려받는다.

### `.env` (프로젝트 루트, git에 올리지 않음)

```
GEMINI_API_KEY=...
SUPABASE_URL=https://<project-id>.supabase.co
SUPABASE_KEY=...            # service_role 키 (서버 전용)
```

선택 항목 (기본값)

| 이름 | 기본값 |
|---|---|
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` |
| `GEMINI_EMBEDDING_DIM` | `768` |
| `SUPABASE_OCR_TABLE` | `ocr_documents` |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | 없음 |
| `MQTT_TLS` | 꺼짐 (`1`이면 TLS) |
| `MQTT_TOPIC_PREFIX` | `ocr` |

### Supabase 테이블 (SQL Editor에서 한 번 실행)

```sql
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
```

### MQTT broker (로컬 테스트용)

```powershell
docker run -d --name ocr-mqtt -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
```

## 2. 서버 실행

```powershell
python run.py
```

## 3. 테스트

**상태 메시지 보기 (대시보드 대신)**

```powershell
docker exec -it ocr-mqtt mosquitto_sub -t "ocr/+/status" -v
```

**PC에서 이미지 한 장 보내기**

```powershell
curl.exe -F session_id=test -F "frame=@C:\path\english_doc.jpg;type=image/jpeg" http://localhost:8000/frame
```

`received → ocr_completed → embedding_completed → stored` 순서로 메시지가 오고 `ocr_documents`에 한 행이 생기면 정상.

**휴대폰 카메라**

```powershell
cloudflared tunnel --url http://localhost:8000
```

- 서버에서 바로: `https://xxxx.trycloudflare.com/camera?session=test`
- GitHub Pages: `https://david-laugh.github.io/Real-time-OCR-Pipeline/?session=test&api=https://xxxx.trycloudflare.com`
  - 루트 `index.html`이 `static/camera/`의 카메라 페이지를 그대로 쓰고, `/frame` 요청만 `api=` 서버로 보낸다.
  - 저장소 Settings → Pages에서 브랜치 / `(root)` 지정 필요. 터널 주소는 실행할 때마다 바뀐다.

## 4. 대시보드 연동 (MQTT)

- 토픽: `ocr/<session_id>/status` (전체 구독: `ocr/+/status`), QoS 1, UTF-8 JSON
- `stage`: `received` / `ocr_completed` / `embedding_completed` / `stored` / `skipped` / `failed`
- `skipped`: 글자가 없는 프레임, 또는 직전에 저장한 문서와 90% 이상 같은 프레임

```json
{
  "type": "ocr_status",
  "stage": "stored",
  "session_id": "test",
  "title": "The Quiet Machinery of Cities",
  "text_preview": "Beneath every pavement there is...",
  "message": "Document stored successfully",
  "document_id": "<uuid>",
  "created_at": "2026-10-27T05:12:33Z"
}
```

`document_id`, `created_at`은 `stored`에만 있다. 본문 전체와 embedding은 Supabase에서 조회한다.
통합 대시보드는 `/ws/dashboard`로 서버에 연결한다. 브라우저용 MQTT 포트는 별도로 필요하지 않다.

### MQTT 본문 및 대시보드 통합

Supabase 저장 성공 후 `ocr/text`에 `{"text":"전체 본문","task_id":"session_id"}`를 추가 발행한다. 서버의 MQTT 수신부가 SQLite에 저장하고 대시보드로 알린다. `MQTT_TOPIC`으로 본문 토픽을 변경할 수 있으며 `MQTT_TLS=1`은 발행·구독 모두에 적용된다.

- `/`: 대시보드, `/viewer`: 문서 검색/뷰어
- 기존 `/camera`, `/frame`, `/pair`, `/session/{session_id}/status` 유지
- `.env.example`을 참고해 `.env`에 Gemini/Supabase/MQTT 설정 입력
- Windows에서는 `python run.py`로 단일 worker 실행
- Supabase와 SQLite는 별도 저장소이며, MQTT 재전송 중복 방지와 두 저장소 간 자동 복구는 아직 없다.

병합 검토 및 테스트 범위는 [RELEASE_MERGE.md](docs/RELEASE_MERGE.md)를 참고한다.

## 5. 파일 구성

| 파일 | 담당 | 역할 |
|---|---|---|
| `main.py` | 공용 | 앱 생성, CORS(github.io), 라우터 등록 |
| `api/camera.py`, `api/frame.py` | 1번 | `GET /camera`, `POST /frame` (즉시 202) |
| `services/frame_queue.py` | 1번 | 세션별 최신 2장 대기 큐, 순차로 `sink.handle()` 호출 |
| `services/frame_sink.py` | 1번/2번 | Sink 인터페이스, `OcrSink`(OCR → embedding → 저장 → MQTT) |
| `static/camera/` | 1번 | 휴대폰 카메라 페이지 |
| `index.html` | 2번 | GitHub Pages 진입점 (`?api=`로 서버 지정) |
| `services/ocr_service.py` | 2번 | EasyOCR, 줄 정렬/제목 분리 |
| `services/embedding_service.py`, `services/gemini_service.py` | 2번 | Gemini embedding |
| `db/supabase.py` | 2번 | `ocr_documents` 저장 |
| `services/mqtt_service.py` | 2번 | MQTT 발행 |

자세한 카메라 페이지 설명은 `docs/mobile-camera-notes.md` 참고.

## 참고

- 단일 프로세스 전제 (`uvicorn` 워커 1개). 대기 큐와 중복 판정 상태는 메모리에만 있다.
- OCR/embedding/Supabase 호출은 `asyncio.to_thread()`로 실행되어 이벤트 루프를 막지 않는다.
- 실패한 프레임은 버리고(`failed` 발행 + 서버 로그), 다음 프레임은 계속 처리한다.
