# release 병합 검토

기준: origin/release `17a9725`, MQTT 원본: pec `4b0f31f`.
브랜치: `codex/mqtt-dashboard-release`.

공용 main.py, requirements.txt, .gitignore, .claude/launch.json 충돌을 해결했다. release의 카메라/프레임/QR 페어링/세션 상태 및 정적 자산을 보존하고 MQTT 대시보드·SQLite·검색 API를 같은 앱에 등록했다.

OCR → Gemini → Supabase 성공 후 `ocr/text`에 전체 본문을 추가 발행한다. 기존 `ocr/<session_id>/status`는 유지한다. 수신부가 SQLite에 저장하고 WebSocket 알림을 전송한다. session_id는 task_id로 매핑된다. MQTT_TLS 설정을 발행과 수신에 동일 적용했다.

SDK import는 실제 호출 시점으로 옮겼다. OCR 실행에는 requirements.txt 전체 의존성과 유효한 Gemini/Supabase 설정이 여전히 필요하다. 서버 종료 시 프레임 큐를 마무리한 뒤 MQTT와 SQLite를 종료한다.

검증: `python -m unittest discover -s tests -v`. 기존 입력 검증 7개와 release 통합 3개(카메라/QR 상태/본문/대시보드 연결, 라우터 및 수명주기, 저장 실패 시 본문 미발행)를 확인한다. OCR/Gemini/Supabase/브로커는 테스트 대역이며 실기기·외부 서비스 검증이 아니다. 테스트 환경은 Windows Python 3.12, FastAPI 0.115.12다.

현재 구현은 QoS 1 재전송 멱등성, 영속 수신 세션, Supabase↔SQLite 원자적 동기화를 제공하지 않는다. 검토 후 release로 병합하며, 이 PR 자체에서 배포나 release 직접 수정은 하지 않는다.
