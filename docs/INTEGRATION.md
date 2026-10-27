# 카메라·OCR·MQTT·대시보드 통합 기록

2026-10-27, 현재 `release` 브랜치(기준 커밋 `2104f71`) 작업 트리에 통합했다. 확인 당시 로컬/원격 추적 브랜치에 `main`은 없었으며 `release`에 모바일 카메라와 OCR 병합 커밋이 있었다. 원격 push, 커밋, 다른 브랜치 merge는 수행하지 않았다.

## 파일 병합

- 기존 카메라 API, 프레임 큐, OCR, Gemini embedding, Supabase, MQTT 상태 발행, GitHub Pages 진입점을 보존했다.
- `새 폴더`의 api/routes, SQLite 모듈, pipeline/search/dedup, MQTT 수신부, static 대시보드·뷰어·이전 보고서, 명세·예제·테스트·실행 설정을 루트로 옮겼다.
- 충돌하는 main.py는 단일 FastAPI 앱과 lifespan으로 합쳤고, requirements.txt와 .gitignore는 기존 항목을 유지하며 필요한 항목을 추가했다. 빈 __init__.py는 동일 파일이라 유지했다.
- 기존 `새 폴더/data/documents.db`는 `data/documents.db`로 보존했다. DB·테스트 설치 패키지·복구 ZIP은 Git에서 제외했다.
- 변경 전 원본은 `.merge-backups/legacy-dashboard-before-merge.zip`에 보관했다. 생성된 __pycache__는 백업 대상에서 제외했다.
- `.claude/launch.json`도 루트로 옮기고 Windows 호환 `run.py`를 실행하도록 갱신했다.

## 실제 연결

`/camera` 또는 GitHub Pages → `POST /frame` → 세션별 큐 → EasyOCR → Gemini embedding → Supabase 저장 → `mqtt_service.publish_document()` → `ocr/text` → `mqtt_handler` → SQLite → `/ws/dashboard` → 대시보드/뷰어.

MQTT 상태 토픽 `ocr/<session_id>/status`는 기존대로 유지한다. 본문 토픽은 `MQTT_TOPIC`으로 발행·구독 양쪽에 적용하며 payload는 `{"text":"전체 본문","task_id":"카메라 session_id"}`다. 상태 이벤트는 미리보기만 포함하므로 본문 입력으로 사용하지 않는다. 원본 제목·UUID·embedding은 Supabase에 보존되고, SQLite 문서 ID·제목은 기존 대시보드 규칙으로 별도 생성한다.

MQTT_TLS=1이면 발행·구독 모두 TLS를 사용한다. 서버 종료 시 받은 프레임 처리가 끝난 뒤 MQTT와 SQLite를 닫는다. 무거운 OCR 및 외부 SDK import는 실제 호출 시점으로 옮겨 대시보드 기동 시 모델/SDK를 불필요하게 로드하지 않도록 했다. 실제 카메라 OCR 실행에는 requirements.txt의 전체 의존성과 API 설정이 필요하다.

## 검증 결과와 범위

- 자동 테스트 12개 통과: 기존 입력 검증 7개 + 통합 5개.
- 통합 테스트는 프레임 수신/큐 → OCR 처리 경계 → 전체 본문 MQTT 발행 → 수신 검증 → SQLite 저장 → WebSocket 이벤트를 확인한다. OCR·embedding·Supabase·브로커는 가짜 구현으로 대체했다.
- 중복 프레임 본문 발행 제외, Supabase 실패 시 본문 미발행, MQTT 발행 실패 시 Supabase 저장 성공 유지, 토픽 변경, 단일 앱의 라우터 및 lifecycle을 검증했다.
- 검증은 Windows Python 3.12에서 저장소 지정 FastAPI 0.115.12/Uvicorn 0.34.3 및 경량 의존성을 `.test-deps`에 설치하여 수행했다. 팀 대상 Python 3.10 및 전체 OCR 의존성 설치는 별도 확인이 필요하다.
- 실제 휴대폰, EasyOCR 모델, Gemini/Supabase 계정, MQTT 브로커를 연결한 종단 테스트는 수행하지 않았다.
- 현재 작업 트리의 충돌 표식·미해결 Git 인덱스와 공백 오류를 검사한다. 이후 다른 브랜치 변경과 병합할 때의 충돌까지 보장하는 것은 아니다.

## 실행

```powershell
python -m pip install -r requirements.txt
# .env가 없다면 .env.example을 복사하고 실제 값을 입력한다.
python run.py
python -m unittest discover -s tests -v
```

`/camera`는 촬영, `/`는 대시보드, `/viewer`는 검색/문서 열람이다. 로컬 테스트 브로커는 `mosquitto -c mosquitto.conf -v`로 별도 실행한다. 자세한 데이터 계약은 [MQTT_OCR_SPEC.md](MQTT_OCR_SPEC.md)를 참조한다.

QoS 1 재전송의 중복 저장, MQTT 오프라인 수신 손실, Supabase와 SQLite 간 불일치는 아직 자동 복구하지 않는다. 안정적 동기화가 필요하면 message_id/영속 outbox/멱등 저장 설계를 후속 적용해야 한다.
