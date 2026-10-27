# 모바일 카메라 페이지 작업 노트 (담당: 1번)

> 새 세션에서 이 문서만 읽고 이어서 작업할 수 있도록 유지한다.
> 마지막 업데이트: 2026-10-27

## 범위
- 포함: 휴대폰에서 여는 `/camera?session=<ID>` 페이지, `POST /frame` 테스트용 목업 수신 API
- 제외(다른 팀원): PC 대시보드, QR 표시, OCR, 임베딩/DB(Supabase), RAG

## 현재 상태 (브랜치 `feature/mobile-camera`)
- [x] 1단계: `.gitignore`, `requirements.txt` — 별도 커밋(팀 공유용)
- [x] `main.py` 골격 — 별도 커밋(팀 공유용)
- [x] 서버: `services/frame_sink.py`, `api/frame.py`, `api/camera.py`, main.py 라우터 등록
- [x] 페이지: `static/camera/index.html`, `camera.css`, `camera.js` (세로/가로 레이아웃 포함)
- [x] PC 크롬 에뮬레이션 테스트(가짜 카메라 스트림) 통과 — 아래 "테스트 기록"
- [x] Python 3.10.11 설치, `.venv` 생성, 서버 실행 확인
- [x] `/frame` 비동기화: 즉시 202 응답 + 세션별 백그라운드 큐(`services/frame_queue.py`)
- [x] `feature/mobile-camera` push (PR은 아직 안 만듦)
- [ ] **터널(cloudflared)로 iPhone Safari / Android Chrome 실기기 테스트** ← 다음 할 일 (체크리스트 아래)
- [ ] release 대상 PR

## 결정사항
| 항목 | 결정 | 이유 |
|---|---|---|
| **팀 Python 버전** | **3.10** | 팀 합의. 3.11+ 전용 문법/표준 라이브러리 사용 금지(`tomllib`, `typing.Self`, `datetime.UTC`, `ExceptionGroup` 등) |
| 서버 | FastAPI + uvicorn | async 업로드 처리, `APIRouter`로 팀원별 모듈 분리, /docs로 계약 공유, OCR 등 Python 생태계 |
| 의존성 버전 | fastapi 0.115.12, uvicorn[standard] 0.34.3, python-multipart 0.0.20 | 3.10.11에서 설치·실행 확인 |
| requirements.txt | **ASCII만 사용(한글 주석 금지)** | Python 3.10 기본 pip 23.0.1이 한글 Windows에서 cp949로 읽다가 UnicodeDecodeError |
| 정적 페이지 서빙 | API와 같은 서버(같은 origin) | CORS 불필요, 터널 하나로 페이지+API 모두 HTTPS |
| 프론트 | 순수 HTML/CSS/JS (빌드 없음) | 요구사항 |
| `/frame` 형식 | `multipart/form-data`: `frame`(JPEG 파일), `session_id`(text) | 2번 팀원과 합의 |
| `/frame` 응답 | **202** `{"ok": true, "queued": true, "dropped": <버린 대기 프레임 수>}` / 400 잘못된 session_id / 413 5MB 초과 / 415 JPEG 아님 / 422 필드 누락 | OCR을 기다리지 않음 |
| 프레임 처리 | `FrameQueue`: 세션별 대기 프레임 최신 2장만 유지(오래된 것 버림), 세션별 워커 1개가 순차로 `sink.handle()` 호출, 예외는 로그만 | OCR이 느려도 휴대폰 전송에 영향 없음, 항상 최신 프레임 처리 |
| session_id 규칙 | `^[A-Za-z0-9_-]{1,64}$` (서버·페이지 동일) | 경로 조작 방지 — QR 담당에게 공유 |
| 교체 구조 | `services/frame_sink.py`의 `get_frame_sink()` 한 줄 교체 | 연결 방법은 파일 맨 위 주석(백그라운드 호출, 블로킹 OCR은 `asyncio.to_thread`) |
| 캡처 크기 | 긴 변 1280px, 원본이 작으면 확대 안 함, `videoWidth/videoHeight` 기준 | 가로/세로 모두 비율 유지 |
| 전송 루프 | 요청 완료 후 `max(0, 2000 - 경과)` 뒤 다음 전송 | 요청 쌓임 방지 |
| 실패 처리 | 실패 프레임 버림, 연속 3회 실패 → "연결 끊김", 1회 성공 → 복구, 요청 타임아웃 8초 | |
| 상수 위치 | `camera.js` 맨 위 (`CAPTURE_INTERVAL_MS`, `MAX_LONG_SIDE_PX`, `JPEG_QUALITY`, `REQUEST_TIMEOUT_MS`, `MAX_CONSECUTIVE_FAILURES`) | |
| 정지 버튼 | 전송 정지 + 카메라 끔(배터리/표시등) | 시작 시 다시 권한 요청 없이 재획득 |
| 백그라운드 | hidden → 루프 정지("일시 정지"), visible → 트랙 끊겼으면 카메라 재오픈 후 재개 | iOS는 백그라운드에서 트랙이 끊김 |
| 가로 화면 | 미리보기 왼쪽 / 컨트롤 오른쪽, "세로로 들면 더 잘 인식돼요" 작게 표시(촬영은 허용) | |
| 인앱 브라우저 | UA 감지 → 안내 + "주소 복사" 버튼만. 카카오톡 자동 열기는 이번엔 제외 | |
| 터널 | cloudflared 우선, ngrok 대안 | 계정 불필요, 경고 페이지 없음 |

## 파일 구성
```
main.py                  (공용) 앱 생성, 담당자별 include_router 자리, /static 마운트
api/camera.py            GET /camera → static/camera/index.html
api/frame.py             POST /frame → 검증 후 FrameQueue.submit(), 즉시 202
services/frame_queue.py  세션별 대기 큐(최신 2장) + 백그라운드 워커 → sink.handle()
services/frame_sink.py   FrameSink 프로토콜, LocalFolderSink(tmp_frames/<session>/<시각>.jpg), get_frame_sink()
static/camera/           index.html, camera.css, camera.js
```

## 실행 방법
### Python 3.10 설치 (없을 때)
```powershell
py install 3.10          # 새 Python 설치 관리자(py) 사용 시
# 또는 https://www.python.org/downloads/release/python-31011/ 에서 Windows installer (64-bit)
py -0p                   # 3.10 표시되는지 확인
```
### 가상환경 / 서버
```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# PC: http://localhost:8000/camera?session=test  (localhost는 HTTPS 없이도 카메라 허용)
# 저장 확인: tmp_frames/test/*.jpg
```
### 휴대폰용 HTTPS 터널
1. **cloudflared (우선)** — 계정 불필요
   ```powershell
   winget install --id Cloudflare.cloudflared
   cloudflared tunnel --url http://localhost:8000
   ```
   출력된 `https://xxxx.trycloudflare.com/camera?session=test` 를 휴대폰에서 열기. 실행할 때마다 주소가 바뀜.
2. **ngrok (대안)**
   ```powershell
   ngrok config add-authtoken <토큰>   # 최초 1회
   ngrok http 8000
   ```
   무료 플랜은 첫 접속 시 경고 페이지 → "Visit Site". fetch에는 `ngrok-skip-browser-warning` 헤더를 이미 넣어 둠.

## 테스트 기록
- 2026-10-27, 임시 Python 3.12 venv(프로젝트 외부)로 확인. 3.10은 미설치 상태라 3.10 실행 확인은 아직.
  - `/camera` 200, `/static/camera/camera.js` 200
  - `/frame`: 정상 200 + tmp_frames 저장, `../x` 400, 비JPEG 415, 필드 누락 422
  - Chrome 모바일 에뮬레이션 + 가짜 카메라 스트림(720×1280):
    - 세로: 2초 간격 전송, 상태 "전송 중", 720×1280 저장(확대 안 함)
    - 가로: 미리보기 왼쪽/컨트롤 오른쪽, 안내 문구 표시
    - fetch 실패 주입 → 3회째 "연결 끊김", 복구 후 "전송 중"
    - NotAllowedError 주입 → Android 안내 표시, "다시 시도"로 새로고침 없이 복구

- 2026-10-27, **Python 3.10.11 `.venv`**:
  - `pip install -r requirements.txt` 성공(ASCII 수정 후), uvicorn 실행
  - `/camera` 200, `/frame` 202 ×3 → tmp_frames 저장 3장, 잘못된 session 400, 비JPEG 415
  - `FrameQueue` 단위 확인(느린 가짜 sink 0.3초): 5장 연속 투입 → 최신 2장만 처리, 예외 프레임 다음 프레임 정상 처리, 끝난 세션 상태 정리됨

## 실기기 테스트 체크리스트 (iPhone Safari / Android Chrome 각각)
- [ ] 첫 접속 시 권한 팝업 → 허용 → 후면 카메라로 미리보기
- [ ] 2초마다 `tmp_frames/<session>/`에 저장, 이미지가 선명한지
- [ ] **세로 촬영 사진이 회전 없이 저장되는지**
- [ ] **가로 촬영 사진이 회전 없이 저장되는지** (긴 변 1280px, 가로 레이아웃·안내 문구 표시)
- [ ] 촬영 중 방향 전환 시 다음 프레임부터 비율이 맞는지
- [ ] 권한 거부 → 안내 문구 → 설정에서 허용 → "다시 시도"로 복구
- [ ] 다른 앱이 카메라 사용 중일 때 NotReadableError 안내
- [ ] 서버 끄기 → "연결 끊김" → 서버 켜기 → 자동 복구
- [ ] 홈 화면 갔다 돌아오기 → "일시 정지" → 자동 재개(iOS 트랙 재오픈 포함)
- [ ] 촬영 중 화면이 꺼지지 않는지(Wake Lock, iOS 16.4+)
- [ ] 카카오톡에서 링크 열기 → 인앱 안내 + 주소 복사 동작
- [ ] http:// 주소(터널 없이 PC IP)로 열기 → "HTTPS 주소로 접속해야 해요" 안내
- [ ] session 없이 `/camera` 열기 → "세션 정보가 없어요", 시작 버튼 비활성

## 팀원에게 공유할 것
- `/frame` 계약(위 표, **202로 변경됨**), `session_id` 규칙 → QR 담당/2번
- requirements.txt에 한글 쓰지 말 것(cp949 오류)
- `main.py`에 담당자별 `include_router` 자리 주석 있음
- QR URL: `https://<터널주소>/camera?session=<세션ID>`
- OCR 연결: `services/frame_sink.py` 맨 위 주석

## 남은 일 / 이슈
- Python 3.10 설치 후 `.venv`에서 실행 재확인
- 실기기 테스트
- ~~OCR 처리 시간이 8초를 넘으면?~~ → 해결: `/frame`이 즉시 202 응답, OCR은 백그라운드 큐에서 처리. `REQUEST_TIMEOUT_MS`(8초)는 업로드 자체에만 적용됨
- 큐는 프로세스 메모리에 있음 → 서버 재시작 시 대기 프레임 유실(최신 2장뿐이라 허용), uvicorn 워커 여러 개로 띄우면 세션별 순서 보장 안 됨(단일 워커 전제)
