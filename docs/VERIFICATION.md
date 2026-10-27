# 통합 실행 검증 — 2026-10-27

## 확인된 결과

- 자동 테스트 12개 재실행 통과.
- 실제 Uvicorn HTTP 서버 시작/종료 통과. `/`, `/viewer`, `/camera`, 카메라 JS, `/openapi.json` 응답 200.
- HTTP 텍스트 저장·빈 입력 422·잘못된 이미지 415 확인.
- 실제 multipart `/frame` 요청 202 → 프레임 큐 → paho 발행 → 로컬 aMQTT 브로커 → aiomqtt 구독 → SQLite 저장 → 실제 WebSocket 이벤트 → HTTP 문서 조회 및 FTS 검색 통과.
- 긴 OCR 본문이 120자 미리보기로 잘리지 않고 전체 저장되는 것을 확인.
- Python 구문 검사, 화면 JavaScript 구문 검사, Git 공백/미해결 인덱스 충돌 검사 통과.
- 테스트용 메모리 DB 사용. 기존 `data/documents.db`는 수정하지 않았고 테스트 서버·브로커는 종료했다.

## 검증하지 못한 부분 / 실행 준비사항

현재 기본 Python 환경에는 EasyOCR, torch, Google GenAI, Supabase 및 python-multipart가 설치되어 있지 않다. `.env` 및 Gemini/Supabase 필수 환경변수도 없다. 따라서 현재 기본 환경에서 곧바로 `python run.py`를 실행하면 multipart 의존성 오류가 발생하며, 실제 이미지 OCR과 클라우드 저장까지 동작하려면 먼저 전체 requirements 설치와 설정이 필요하다.

이번 검증은 `.test-deps`의 FastAPI 0.115.12·Uvicorn 0.34.3·multipart 등 경량 의존성을 사용했다. 로컬 브로커는 `.broker-deps`에 설치한 aMQTT 0.11.3이며 MQTT 네트워크 통신은 실제 수행했다. OCR 인식 결과, Gemini embedding, Supabase 저장 응답만 테스트 대역으로 교체했다.

실제 휴대폰 촬영 권한/HTTPS 전송, 화면의 브라우저 렌더링·사용자 조작, EasyOCR 모델 인식 품질, Gemini API, Supabase 테이블·권한·vector 차원, 배포 브로커 인증/TLS 및 장시간 재연결은 별도 실환경 검증이 필요하다. 재전송 중복 저장과 두 DB 간 자동 복구 미지원도 그대로다.

## 재현

전체 애플리케이션 의존성을 설치한 개발 환경에서:

```powershell
python -m unittest discover -s tests -v
```

현재 PC에 준비한 격리 테스트 패키지로 네트워크 검증 재현:

```powershell
$env:PYTHONPATH = "$(Join-Path (Get-Location) '.test-deps');$(Join-Path (Get-Location) '.broker-deps')"
python tests/smoke_network.py
```

네트워크 테스트는 loopback의 임시 포트만 사용하며 API 비용·실제 클라우드 문서 저장이 발생하지 않는다.
