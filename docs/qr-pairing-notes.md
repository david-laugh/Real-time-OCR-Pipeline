# QR 연동 작업 노트 (담당: 1번)

> 새 세션에서 이 문서만 읽고 이어서 작업할 수 있도록 유지한다.
> 카메라 페이지 관련 기록은 `docs/mobile-camera-notes.md`.
> 마지막 업데이트: 2026-10-27

## 목표
PC 대시보드에 QR을 띄우고, 휴대폰이 QR을 찍으면 카메라 페이지(`/camera?session=<ID>`)로 연결한다.
대시보드 전체는 다른 팀원 담당일 수 있으므로 QR은 **독립 조각(`<qr-pairing>`)**으로 만들고, 단독 테스트 페이지 `/pair`를 둔다.

## 현재 상태 (브랜치 `feature/qr-pairing`, 기준 `origin/release` 308f855 = PR #1 머지 후)
- [x] 브랜치 생성, 노트 작성
- [ ] 서버: 세션 상태 기록 + 상태 조회 API
- [ ] QR 라이브러리 파일 추가
- [ ] `<qr-pairing>` 조각
- [ ] `/pair` 단독 페이지
- [ ] 로컬 확인 → 터널로 실기기 QR 스캔 확인
- [ ] push, PR(→ release)

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
| main.py | 라우터 2줄만 추가 | 승인됨 |
| CORS | 수정 안 함, PR에 메모 | 대시보드가 github.io에서 열리면 `allow_methods`에 GET 추가 필요 |

## 2번 팀원과 맞출 점
- `static/camera/index.html`(1번)의 마크업 id가 바뀌면 **루트 `index.html`(GitHub Pages용 껍데기)도 같이 맞춰야 함**. 루트 페이지는 내 `camera.js`/`camera.css`를 불러 쓰기만 함
- 현재 루트 `index.html`에는 진단 표시 요소 `<pre id="debug">`가 없음 → 그 페이지를 `?debug=1`로 열면 오류. 평소 사용에는 영향 없음
- 루트 `index.html`은 `camera.css`/`camera.js`를 `?v=` 없이 불러옴 → 캐시 때문에 옛 버전이 뜰 수 있음
- **머지 순서 제안: PR #1(카메라, 머지 완료) → `feature/ocr` → QR(`feature/qr-pairing`)**
  - `feature/ocr`과 QR 브랜치가 모두 `main.py`의 같은 영역(라우터 등록 부근)을 수정 → QR을 나중에 머지하며 충돌 해결
- 대시보드는 `<qr-pairing>`의 `session-change` 이벤트로 현재 세션 ID를 받아 MQTT `ocr/<session_id>/status` 구독에 쓰면 됨

## 남은 일 / 이슈
- (진행하며 기록)
