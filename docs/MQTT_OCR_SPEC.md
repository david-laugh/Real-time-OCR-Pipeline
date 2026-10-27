# OCR MQTT 수신 명세 및 개발자 전달 자료

작성일: 2026-10-27. 이 저장소의 수정된 구현 기준 명세이며 외부 OCR 송신 시스템과 합의된 명세는 아니다.

## 개발자에게 전달할 핵심

OCR 완료 텍스트를 UTF-8 JSON으로 `ocr/text` 토픽에 발행한다. `text`는 필수 문자열, `task_id`는 선택 문자열이며 생략하면 `default`로 저장된다. 같은 작업의 페이지를 같은 `task_id`로 묶고 메시지 하나를 문서 하나로 보낸다. 발행은 QoS 1, retain=false를 사용한다. 수신 서버는 SQLite 저장 후 대시보드 WebSocket으로 알린다. MQTT 저장 완료 응답 토픽과 재전송 중복 방지는 현재 없다. 이미지, 좌표, 신뢰도는 이 MQTT 입력의 범위에 포함되지 않는다. 카메라 이미지는 별도 HTTP `/frame`으로 받아 OCR 처리한다.

## 폴더와 처리 흐름

| 파일/폴더 | 역할 |
| --- | --- |
| `main.py` | FastAPI 초기화, MQTT 시작·종료, 환경변수 로드 |
| `run.py` | Windows 호환 이벤트 루프로 단일 서버 실행 |
| `services/mqtt_handler.py` | MQTT 구독, JSON 검증, 파이프라인 호출, 알림 |
| `services/ocr_message.py` | MQTT·HTTP 공통 입력 검증 모델 |
| `services/pipeline.py` | 공백 정리, 중복 필터 호출, ID·제목 생성, 저장 |
| `services/dedup.py` | 유사도 필터. 현재 main.py에서 비활성화 |
| `db/meta_store.py` | SQLite documents 및 FTS5 검색 인덱스 |
| `api/routes.py` | HTTP 입력, 문서·작업 조회, 검색, WebSocket |
| `static/` | dashboard.html, viewer.html, 기존 report.html |

카메라 `/frame` → OcrSink(EasyOCR → Gemini embedding → Supabase 저장) → mqtt_service → MQTT 브로커 → mqtt_handler → 검증 → Pipeline → SQLite → WebSocket 순서다. 외부 OCR 발행자도 동일한 본문 토픽을 사용할 수 있다. 기존 `static/report.html`은 이전 HTTP 중심 보고서이므로 MQTT 연동은 이 문서를 기준으로 한다.

통합 후 `mqtt_service.py`가 **발행**, `mqtt_handler.py`가 **구독**을 담당한다. Supabase 저장 성공 시 본문을 `MQTT_TOPIC`(기본 `ocr/text`)에 발행하며 카메라 session_id를 task_id로 매핑한다. `ocr/<session_id>/status`는 진행 상태와 120자 미리보기용으로 유지하며, 본문 수신부는 이 상태 토픽을 구독하지 않는다. 저장 실패·빈 OCR·중복 프레임은 본문을 발행하지 않는다. MQTT 본문 전송 실패가 Supabase 저장 성공을 취소하지 않으며, SQLite와 Supabase 간 자동 복구는 없다.

## 연결 계약

| 항목 | 값 |
| --- | --- |
| 프로토콜 | MQTT 3.1.1, TCP |
| 기본 브로커 | localhost:1883 (서버 기준 주소) |
| 기본 수신 토픽 | `ocr/text`, 환경변수로 변경 가능 |
| 구독 QoS | 1 |
| 송신 권장 QoS | 1 (송신 QoS 0은 전달 보장이 더 낮음) |
| retain | false 필수. 재구독 시 retain 표시된 메시지는 무시 |
| 본문 | UTF-8 JSON 객체, 메시지 하나 = 문서 하나 |
| 장애 재연결 | 연결 오류 후 3초 대기, 다시 연결·구독 |
| 인증 | 사용자명·비밀번호 설정 가능. MQTT_TLS=1이면 발행·구독 모두 시스템 신뢰 인증서로 TLS 사용 |

`mosquitto.conf`는 1883 포트의 익명 접속용 개발 설정이다. 실제 배포 브로커 주소·접근망·인증 방식은 담당자와 확정해야 한다. TLS 브로커에서는 MQTT_TLS=1과 해당 MQTT_PORT(통상 8883)를 함께 설정한다.

## JSON 필드

```json
{
  "text": "첫 번째 페이지 제목\nOCR로 인식한 본문입니다.",
  "task_id": "scan-20261027-001"
}
```

| 필드 | 타입 | 필수 | 검증·처리 |
| --- | --- | --- | --- |
| text | string | 예 | 앞뒤 공백 제거 후 1자 이상. 숫자·배열·null 불가 |
| task_id | string | 아니요 | 생략 시 default. 명시 시 앞뒤 공백 제거 후 1자 이상. null 불가 |

줄바꿈은 JSON에서 `\n`으로 표현한다. 내부 줄바꿈과 공백은 유지한다. 작업별 조회를 위해 작업 ID에는 URL에 사용하기 편한 영문·숫자·하이픈을 권장한다. 현재 명시적인 최대 길이 제한은 없으며 브로커 제한이 적용될 수 있다. 이미지/base64나 대용량 배치는 보내지 않는다.

추가 필드는 호환성을 위해 무시하며 저장하지 않는다. `source_id` 컬럼은 DB에 존재하지만 현재 수신 경로에서 매핑하지 않는다. `message_id`, `timestamp`, `page`, `confidence`, `bbox`를 보내도 저장·중복 방지·정렬에 사용하지 않는다. 잘못된 JSON/UTF-8, 객체가 아닌 JSON, 필드 누락·타입 오류·빈 문자열은 경고 로그 후 버리고 다음 메시지를 계속 처리한다. MQTT 오류 응답은 발행하지 않는다. HTTP `POST /api/process`도 같은 필드 검증을 사용하며 잘못된 입력에 422를 반환한다.

## 저장 및 알림

서버가 `doc_001` 형태의 doc_id를 생성한다. 제목은 정리된 본문의 첫 줄 최대 60자, char_count는 Python 문자열 길이(바이트 수 아님)다. DB created_at은 서버 SQLite 저장 시각(UTC), 이벤트 timestamp는 서버 처리 시각(UTC ISO 8601)이다. 송신 측 촬영·인식 시각은 저장하지 않는다.

`/ws/dashboard`에 다음 두 이벤트를 보낸다. 이 채널은 MQTT 응답 채널이 아니다.

```json
{"type":"new_document","data":{"doc_id":"doc_001","task_id":"scan-20261027-001","title":"OCR 결과","char_count":6,"timestamp":"2026-10-27T05:00:00+00:00"}}
```

```json
{"type":"stats","data":{"total":1}}
```

문서 본문은 `GET /api/documents/{doc_id}`, 작업별 본문은 `GET /api/tasks/{task_id}/docs`, 목록은 `GET /api/documents`, 통계는 `GET /api/stats`로 확인한다. WebSocket 미접속/알림 실패 후에는 HTTP 조회로 저장 상태를 확인한다.

## 전달 보장과 운영 제약

- QoS 1은 DB 저장 성공 확인이 아니다. MQTT ACK와 애플리케이션 저장은 별개이며 처리 실패 시 서버가 자동 재처리하지 않는다.
- 현재 dedup은 꺼져 있다. 같은 메시지를 재전송하면 새 문서가 생긴다. 켜더라도 텍스트 유사도 필터이지 message_id 기반 멱등성 보장이 아니다.
- 영속 MQTT 세션·오프라인 적재를 구성하지 않았다. 서버가 연결되지 않은 동안 발행된 메시지는 복구를 보장하지 않는다.
- 여러 발행자 사이의 페이지 순서·전역 순서를 보장하지 않는다. 페이지 번호 필드가 없다.
- 단일 서버 프로세스로 실행한다. 여러 worker/인스턴스는 같은 토픽을 각각 수신하여 중복 처리하고 doc_id 충돌을 일으킬 수 있다.
- retain=false는 송신자가 지켜야 한다. 이미 구독 중인 소비자에게는 retained 발행도 retain 표시 없이 도착할 수 있어 수신 필터만으로 완전 차단할 수 없다.

저장 확인과 무손실 재전송이 요구되면 별도 합의 후 `message_id` 유일 키, 처리 결과 응답 토픽, 송신 측 재시도/보관, 영속 세션을 함께 설계해야 한다.

## 실행 및 송신 확인

Python 3.10 이상에서 저장소 루트를 작업 폴더로 사용한다. 검증 환경은 Windows/Python 3.12다.

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# .env의 MQTT_HOST 등을 실제 브로커에 맞게 수정
mosquitto -c mosquitto.conf -v
```

다른 터미널에서:

```powershell
python run.py
```

Windows에서는 aiomqtt가 add_reader를 지원하는 이벤트 루프를 요구하므로 `run.py`를 사용한다. [aiomqtt 공식 문서](https://aiomqtt.bo3hm.com/)의 Windows 실행 지침을 적용했다. 재로딩이나 여러 worker 옵션은 사용하지 않는다.

서버 로그의 `MQTT connected, subscribed to ocr/text`를 확인한 뒤 Mosquitto 클라이언트가 설치된 터미널에서:

```powershell
mosquitto_pub -h localhost -p 1883 -t ocr/text -q 1 -f examples/ocr-message.json
Invoke-RestMethod http://localhost:8000/api/tasks/scan-20261027-001/docs
```

retain 옵션 `-r`을 붙이지 않는다. 이 명령은 기본 익명 로컬 브로커 기준이다. 원격 브로커 사용 시 송신자와 서버가 동일한 호스트·포트·토픽·인증을 사용해야 한다. 대시보드는 `http://localhost:8000/`, 뷰어는 `/viewer`다.

## 수정 및 검증 기록

기존 작업 중인 MQTT 연결 코드를 유지하면서 aiomqtt 의존성, 접속 환경변수, 공통 필드 검증, QoS 1 구독, retained 재생 무시, 메시지 단위 예외 격리, Windows 실행 진입점을 추가했다. 빈 텍스트의 DB 저장을 방지하고 REST에도 동일한 입력 규칙을 적용했다.

```powershell
python -m unittest discover -s tests -v
```

2026-10-27 검증 결과: 자동 테스트 7개 통과, Python 구문 검사 및 git diff 공백 검사 통과. 테스트는 메모리 DB와 가짜 MQTT 클라이언트로 정상 한글 저장·알림, 기본값, 잘못된 입력 후 정상 처리, retained 제외, 재전송 중복 특성, 처리 오류 후 구독 지속, HTTP 검증을 확인했다. 샌드박스에서는 asyncio 내부 소켓 생성이 멈춰 승인된 샌드박스 외 실행으로 검증했다. 실제 브로커 연결·장치 송신 검증은 별도로 필요하다.
