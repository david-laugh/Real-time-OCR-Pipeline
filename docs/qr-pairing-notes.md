# QR 연동 작업 노트 (담당: 1번)

> 새 세션에서 이 문서만 읽고 이어서 작업할 수 있도록 유지한다.
> 카메라 페이지 관련 기록은 `docs/mobile-camera-notes.md`.
> 마지막 업데이트: 2026-10-27

## 목표
PC 대시보드에 QR을 띄우고, 휴대폰이 QR을 찍으면 카메라 페이지(`/camera?session=<ID>`)로 연결한다.
대시보드 전체는 다른 팀원 담당일 수 있으므로 QR은 **독립 조각(`<qr-pairing>`)**으로 만들고, 단독 테스트 페이지 `/pair`를 둔다.

## 현재 상태 (브랜치 `feature/qr-pairing`, 기준 `origin/release` 308f855 = PR #1 머지 후)
- [x] 브랜치 생성, 노트 작성 (PR #1이 이미 머지되어 release 기준으로 시작)
- [x] 서버: `services/session_registry.py`, `api/session.py`, `api/pair.py`, `/frame` 1줄, `main.py`
- [x] QR 라이브러리 `static/vendor/qrcode/` (qrcode-generator 1.4.4, MIT)
- [x] `<qr-pairing>` 조각 `static/pair/qr-pairing.js`
- [x] `/pair` 단독 페이지 `static/pair/index.html`
- [x] 로컬 확인 (아래 "테스트 기록")
- [x] push, PR(→ release)
- [ ] **터널로 실기기 QR 스캔 → "연결됨" 확인** ← 다음 할 일 (아래 "실기기 확인 방법")

## 결정사항
| 항목 | 결정 | 이유 |
|---|---|---|
| 형태 | 커스텀 태그 `<qr-pairing>` + Shadow DOM, 스크립트 1개 | 대시보드 CSS와 간섭 없이 붙이기 쉬움 |
| 세션 ID | 브라우저에서 `crypto.getRandomValues`로 8자(헷갈리는 0/o/1/l 제외 소문자+숫자) | 기존 규칙 `^[A-Za-z0-9_-]{1,64}$` 만족 |
| ID 유지 | 같은 탭 새로고침 시 유지(`sessionStorage`), "새 세션" 버튼으로만 교체 | 승인됨 |
| QR 대상 기본값 | 같은 서버 `<origin>/camera?session=<ID>` | 승인됨 |
| GitHub Pages 대상 | `camera-url` 속성 템플릿으로 선택 가능 | 2번 팀원 루트 index.html 사용 시. 페이지는 합치지 않음 |
| 연결 판정 | 프레임 없음 = 대기 중, 마지막 프레임 6초 이내 = 연결됨, 그 외 = 연결 끊김 | 전송 간격 2초 × 3. 승인됨 |
| 수신 시각 기록 | `/frame` 검증 통과 시점(OCR 결과와 무관) | |
| QR 생성 | `qrcode-generator`(MIT) 파일을 `static/vendor/qrcode/`에 포함 | 외부 서비스·CDN 없이 동작, requirements.txt 변경 없음 |
| main.py | 라우터 2줄 + import 줄에 `pair, session` 추가 | import 없이는 등록 불가 |
| 상태 API | `GET /session/{id}/status` → `{session_id, state, last_frame_at, seconds_since_last, frames_received}` | 잘못된 id는 400. 세션은 메모리, 1시간 프레임 없으면 정리 |
| 서버 응답 없음 | 상태 조회 실패 시 조각에 "서버 응답 없음"(`unreachable`) 표시 | |
| 이벤트 순서 | `session-change` → `status-change(waiting)` 순 | 대시보드가 ID를 먼저 받도록 |
| CORS | 수정 안 함, PR에 메모 | 대시보드가 github.io에서 열리면 `allow_methods`에 GET 추가 필요 |

## 2번 팀원과 맞출 점
- `static/camera/index.html`(1번)의 마크업 id가 바뀌면 **루트 `index.html`(GitHub Pages용 껍데기)도 같이 맞춰야 함**. 루트 페이지는 내 `camera.js`/`camera.css`를 불러 쓰기만 함
- 현재 루트 `index.html`에는 진단 표시 요소 `<pre id="debug">`가 없음 → 그 페이지를 `?debug=1`로 열면 오류. 평소 사용에는 영향 없음
- 루트 `index.html`은 `camera.css`/`camera.js`를 `?v=` 없이 불러옴 → 캐시 때문에 옛 버전이 뜰 수 있음
- **머지 순서 제안: PR #1(카메라, 머지 완료) → `feature/ocr` → QR(`feature/qr-pairing`)**
  - `feature/ocr`과 QR 브랜치가 모두 `main.py`의 같은 영역(라우터 등록 부근)을 수정 → QR을 나중에 머지하며 충돌 해결
- 대시보드는 `<qr-pairing>`의 `session-change` 이벤트로 현재 세션 ID를 받아 MQTT `ocr/<session_id>/status` 구독에 쓰면 됨

## 파일
```
services/session_registry.py   per-session last frame time / state (memory)
api/session.py                 GET /session/{id}/status
api/pair.py                    GET /pair
api/frame.py                   registry.mark_frame(session_id) 1줄
static/pair/qr-pairing.js      <qr-pairing> 조각
static/pair/index.html         /pair 테스트 페이지 (?pages=1, ?camera-url=, ?api-base=)
static/vendor/qrcode/          qrcode.js + LICENSE
```

## 대시보드에 붙이는 방법
```html
<script src="/static/pair/qr-pairing.js" defer></script>
<qr-pairing></qr-pairing>
<!-- GitHub Pages 카메라 페이지로 보낼 때 -->
<qr-pairing camera-url="https://david-laugh.github.io/Real-time-OCR-Pipeline/?session={session}&api={api}"></qr-pairing>
<script>
  const qr = document.querySelector("qr-pairing");
  qr.addEventListener("session-change", (e) => { /* e.detail.sessionId → MQTT ocr/<id>/status 구독 */ });
  qr.addEventListener("status-change", (e) => { /* e.detail.state */ });
</script>
```
- 속성: `camera-url`(템플릿 `{session}`, `{api}`), `api-base`(기본 현재 origin), `poll-ms`(기본 2000)
- 속성/메서드: `qr.sessionId`, `qr.state`, `qr.cameraUrl`, `qr.newSession()`
- 리스너를 늦게 붙이면 처음 이벤트를 놓칠 수 있으니 `qr.sessionId`로 현재 값을 읽을 것
- 스타일은 Shadow DOM 안에 있음. 크기는 `--qr-pairing-size`(기본 240px) 등 CSS 변수로 조정
- 대시보드가 다른 origin(github.io 등)에서 열리면 `api-base`에 터널 주소를 넣고, 서버 CORS `allow_methods`에 GET 추가 필요(현재 POST만)

## 테스트 기록
- 2026-10-27, Python 3.10 `.venv`, 로컬:
  - 상태 판정 단위 확인(가짜 시계): 프레임 없음 waiting → 5.9초 connected → 6.1초 disconnected → 새 프레임 connected, 1시간 후 정리
  - HTTP: 새 세션 waiting, `/frame` 202 후 connected, 잘못된 id 400
  - `/pair`(Chrome): QR(SVG 41×41) 표시, localhost 경고, 새로고침 시 ID 유지, "새 세션"으로 ID·QR 교체
  - 프레임 4장 전송 → "연결됨" → 마지막 프레임 약 6초 후 "연결 끊김", 이벤트 로그 확인
  - `?pages=1` → `https://david-laugh.github.io/Real-time-OCR-Pipeline/?session=<id>&api=<origin>`
  - 서버 종료 → "서버 응답 없음"
  - `feature/ocr`과 머지 시뮬레이션(`git merge-tree`): **충돌 없음**, main.py에 CORS와 라우터 등록이 함께 들어감
- QR 디코딩은 PC에서 확인 못 함 → 실기기 스캔으로 확인

## 실기기 확인 방법
1. PC: `.\.venv\Scripts\Activate.ps1` → `uvicorn main:app --host 0.0.0.0 --port 8000`
2. PC: `cloudflared tunnel --url http://localhost:8000` → 출력된 `https://xxxx.trycloudflare.com`
3. PC 브라우저: `https://xxxx.trycloudflare.com/pair` (localhost 경고가 **없어야** 정상)
4. 휴대폰 기본 카메라 앱으로 QR 스캔 → Safari/Chrome에서 `/camera?session=<같은 ID>` 열림 → "시작"
5. PC 화면: "대기 중" → 2~4초 안에 "연결됨"(프레임 수 증가)
6. 휴대폰 "정지" → 약 6초 뒤 "연결 끊김"
7. PC "새 세션" → QR 바뀜 → 다시 스캔해서 새 ID로 "연결됨"
8. (선택) `/pair?pages=1` → QR이 github.io 카메라 페이지로 연결. 2번 팀원 PR(feature/ocr: CORS, 루트 index.html) 머지 후에만 동작

## 남은 일 / 이슈
- 실기기 QR 스캔 확인
- 대시보드 담당자에게 붙이는 방법 공유
- 세션 상태는 서버 메모리 → 재시작 시 "대기 중"으로 돌아감(휴대폰이 계속 보내면 곧 "연결됨"), 단일 워커 전제
