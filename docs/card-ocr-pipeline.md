# 카드 추적 기반 OCR 파이프라인 (RapidOCR + spaCy + MQTT)

PoC(`mobile_ocr_server_with_sentence_parsing.py`)에서 동작을 확인한 카드 검출 → 추적 → trigger → RapidOCR → 문장 분리 흐름을 기존 `/frame` → `frame_sink.handle()` 구조에 옮겼다. PoC 파일은 분석용으로만 썼고, production 코드는 그 파일을 import하지 않는다. 삭제해도 동작한다.

이번 작업 범위는 OCR 처리 부분이다. `/frame` API, 카메라 페이지, QR 페어링, 대시보드 화면은 수정하지 않았다.

## 1. 변경한 파일

| 파일 | 변경 내용 |
|---|---|
| `services/frame_sink.py` | `OcrSink` 내부를 교체했다. 카드 검출(`detect_document_cards`, `cluster_vertical_edges`, `find_dominant_card_band`), `Track`/`CardTracker`, `is_fully_visible`, `crossed_trigger_line`, `crop_card`, `SessionState`, `process_frame`, 세션별 OCR 큐/worker, 문장 발행(`publish_sentences`)을 추가했다. 기존 `FrameSink` Protocol, `LocalFolderSink`, `get_frame_sink()`는 그대로 두었다. 서버 기동/종료용 `startup()`, `shutdown()`을 추가했다. |
| `services/ocr_service.py` | EasyOCR 대신 RapidOCR를 쓴다. `prepare_ocr_image`, `run_rapidocr`, `normalize_ocr_text`, `parse_ocr_document`(제목 + spaCy 문장), `read_card`, `warmup`이 있다. 기존 `extract_text(jpeg_bytes)` 인터페이스는 RapidOCR 기반으로 유지했다. |
| `services/mqtt_service.py` | `sentence_topic(session_id)`를 추가하고 docstring에 토픽을 정리했다. client는 새로 만들지 않고 기존 paho client를 재사용한다. |
| `main.py` | lifespan에 두 줄을 추가했다. 기동 시 `frame_sink.startup()`(모델 백그라운드 로드), 종료 시 `await frame_sink.shutdown()`(대기 중인 카드 OCR/발행 마무리 후 MQTT 종료). |
| `requirements.txt` | `easyocr`를 빼고 `rapidocr-onnxruntime`, `opencv-python`, `numpy`, `spacy`를 추가했다. |
| `.env.example` | `OCR_TRIGGER_RATIO=0.55`를 추가했다. |
| `tests/test_card_ocr_pipeline.py` | 새 테스트 17개 |
| `tests/test_release_integration.py` | 삭제된 EasyOCR → embedding → Supabase 경로를 patch하던 테스트 2개를 새 흐름으로 바꿨다. |
| `README.md` | OCR/MQTT 서술 중 바뀐 부분만 고쳤다. |

## 2. 실제 runtime 데이터 흐름

```
휴대폰 카메라 ── POST /frame ──▶ api/frame.py ── 즉시 202
                                     │
                     services/frame_queue.py (기존, 수정 없음)
                     세션별 최신 2장만 대기 (밀리면 오래된 프레임 버림), 같은 세션은 순차 처리
                                     │
                     frame_sink.OcrSink.handle(session_id, jpeg_bytes)
                     ├─ SessionState (세션마다 독립: tracker, frame_index, card_count, OCR 큐)
                     └─ asyncio.to_thread(process_frame)
                          JPEG → numpy → detect_document_cards → CardTracker.update
                          → track마다: PARTIAL / FULLY_VISIBLE 판별 → trigger 선 통과 판별
                          → 조건 충족 시 track.processed = True → crop → CardJob
                     → CardJob을 세션 OCR 큐에 넣고 바로 반환 (OCR을 기다리지 않음)
                                     │
                     세션 OCR worker (세션마다 1개, 카드가 trigger를 넘은 순서대로)
                     └─ asyncio.to_thread(ocr_service.read_card)
                          prepare_ocr_image (외곽 1.2% 제거, 높이 1400 미만이면 확대)
                          → RapidOCR (프로세스 공용 엔진 1개 + lock)
                          → 줄을 위→아래 정렬 → 첫 줄 = title, 나머지 줄은 공백으로 이어 body
                          → spaCy sentence segmentation → sentences
                     └─ MQTT 발행 (기존 mqtt_service)
                          ① ocr/<session_id>/sentence  문장 1개당 1건, 원문 순서
                          ② ocr/text                   카드 1장당 1건 "title\nbody" (대시보드 기존 계약)
                          ③ ocr/<session_id>/status    카드 1장당 1건 ocr_completed / skipped / failed
```

OCR 조건은 PoC 의미를 그대로 유지한다.

```python
not track.processed and track.ever_fully_visible and full_visible and crossed
```

- `crossed`는 trigger 근처에 있는지가 아니라, 직전 프레임 중심점과 현재 중심점이 trigger 선의 양쪽에 있는지로 판단한다. 왼쪽→오른쪽, 오른쪽→왼쪽 모두 지원한다.
- `track.processed = True`는 OCR 큐에 넣기 전에 설정한다. 같은 카드가 큐에 두 번 들어가지 않는다.
- 화면 끝에 걸린(PARTIAL) 카드는 `full_visible`이 False라서 trigger를 넘어도 OCR하지 않는다.

### Frame queue를 하나만 둔 이유

PoC는 `/frame`에서 세션별 `asyncio.Queue(maxsize=2)`에 넣고 frame worker가 꺼내 처리했다. 현재 프로젝트에서는 기존 `services/frame_queue.py`가 같은 역할(세션별 최신 2장 유지, 오래된 프레임 버림, 같은 세션 순차 처리)을 이미 하고 있다. 그래서 `handle()` 안에 큐를 하나 더 만들지 않고 이 큐를 frame queue로 썼다. `handle()`은 검출·추적만 thread에서 수행하고(실제 영상 1920px JPEG 기준 프레임당 중앙값 48ms, 최대 56ms), 오래 걸리는 RapidOCR은 별도 OCR 큐/worker로 넘긴다. 같은 세션 안에서 순서를 한 번 더 보장하려고 `SessionState.frame_lock`도 걸어 두었다.

### Thread safety
- RapidOCR 엔진은 프로세스에서 하나만 만들고, 생성과 추론을 `_engine_lock`으로 직렬화한다. 여러 세션이 동시에 OCR해도 안전하다.
- spaCy 파이프라인도 하나만 만들고 `_nlp_lock`으로 보호한다.
- tracker 상태는 해당 세션의 `handle()`만 변경한다. OCR worker는 crop 이미지만 다룬다.

## 3. MQTT topic과 payload

기존 프로젝트에 MQTT publisher(`services/mqtt_service.py`, paho-mqtt, QoS 1, retain 없음, 자동 재연결)와 `MQTT_*` 환경변수 규칙이 이미 있어서 그대로 재사용했다. 문장 토픽은 기존 status 토픽과 같은 `MQTT_TOPIC_PREFIX` 규칙을 따른다.

| 토픽 | 단위 | 내용 |
|---|---|---|
| `<MQTT_TOPIC_PREFIX>/<session_id>/sentence` (기본 `ocr/<id>/sentence`) | 문장 1개 | **이번 작업의 최종 출력** |
| `<MQTT_TOPIC>` (기본 `ocr/text`) | 카드 1장 | `{"text": "title\nbody", "task_id": session_id}`. 기존 대시보드 수신 계약을 유지한다(첫 줄이 문서 제목이 된다). |
| `<MQTT_TOPIC_PREFIX>/<session_id>/status` | 카드 1장 | `ocr_status` 이벤트(`ocr_completed` / `skipped` / `failed`) |

전체 구독: `ocr/+/sentence`, `ocr/+/status`

### sentence payload

```json
{
  "type": "ocr_sentence",
  "message_id": "white:3:2",
  "session_id": "white",
  "track_id": 3,
  "card_index": 1,
  "title": "FIELD NOTES:MIGRATORY BIRDS",
  "sentence_index": 2,
  "sentence": "The species winters as far as 9,ooo kilometres from its breeding grounds.",
  "ocr_confidence": 0.9683,
  "captured_at": 1793118592.974
}
```

| 필드 | 설명 |
|---|---|
| `message_id` | `<session_id>:<track_id>:<sentence_index>`. 같은 입력이면 항상 같은 값이 나오므로 downstream에서 중복을 걸러낼 때 쓴다. |
| `track_id` | 세션 안에서 카드 추적 번호(1부터). 세션마다 따로 센다. |
| `card_index` | 세션에서 몇 번째로 OCR한 카드인지(1부터) |
| `title` | 카드의 첫 번째 OCR 줄. 문장 목록에는 넣지 않고 각 문장의 metadata로 붙인다. |
| `sentence_index` | 카드 안에서 문장 순서(0부터) |
| `ocr_confidence` | RapidOCR 줄 confidence 평균(없으면 `null`) |
| `captured_at` | 카드가 trigger를 통과한 시각(epoch 초) |

- 발행 순서는 원문 문장 순서와 같다. 한 카드의 문장은 같은 OCR worker가 하나씩 발행한다.
- body 문장이 없으면(빈 결과, 제목만 있음) sentence 메시지를 보내지 않고 status `skipped`만 보낸다.
- publish 실패는 메시지 단위로 잡아 로그만 남기고 다음 문장으로 넘어간다. broker가 끊겨 있으면 paho가 QoS 1 메시지를 큐에 넣어 두었다가 재연결 후 보낸다. MQTT 오류 때문에 frame/OCR worker가 멈추지 않는다.
- host, port, 계정, TLS, prefix는 모두 기존 `.env` 변수(`MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS`, `MQTT_TOPIC_PREFIX`, `MQTT_TOPIC`)를 쓴다.

## 4. Dependency

`requirements.txt` (Python 3.10)

| 패키지 | 비고 |
|---|---|
| `rapidocr-onnxruntime==1.4.4` | 팀 venv와 `ocr_bench.py`에서 쓰던 버전. onnxruntime을 함께 설치한다. 코드는 신형 `rapidocr`(>=2) 패키지도 지원한다. |
| `opencv-python>=4.8`, `numpy>=1.24,<3` | 카드 검출과 JPEG decode에 쓴다. rapidocr의 의존성과 같은 패키지다. |
| `spacy>=3.7,<4` | 문장 분리. 검증 버전은 3.8.18. |
| (선택) `en_core_web_sm` | `python -m spacy download en_core_web_sm`. 없으면 `spacy.blank("en")` + `sentencizer`로 대신하며 서버는 정상 동작한다. |
| ~~`easyocr==1.7.2`~~ | 삭제했다. production에서 더 이상 쓰지 않는다(torch 1~2GB 설치가 사라진다). `ocr_bench.py`로 엔진을 비교할 때만 `pip install easyocr`로 따로 설치한다. |

## 5. 실행 / 테스트 방법

### 서버
```powershell
pip install -r requirements.txt
python -m spacy download en_core_web_sm   # 선택
python run.py
```
기동 로그에 `RapidOCR engine loaded`, `Sentence segmentation: en_core_web_sm parser`(또는 sentencizer fallback 경고)가 찍히면 모델 로드가 끝난 것이다. 모델은 백그라운드에서 로드되므로 서버 기동을 막지 않는다.

### MQTT 확인
```powershell
docker exec -it ocr-mqtt mosquitto_sub -t "ocr/+/sentence" -t "ocr/+/status" -t "ocr/text" -v
```

### 단위/통합 테스트 (RapidOCR·MQTT mock, 합성 프레임으로 실제 OpenCV 검출/추적)
```powershell
python -m unittest discover -s tests
```

| 검증 항목 | 테스트 |
|---|---|
| 같은 카드는 1회만 OCR, trigger 이후 20프레임 넘게 보여도 재발행 없음, 다음 카드는 새 track | `test_same_card_read_once_next_card_is_new` |
| 왼쪽→오른쪽 방향 | `test_left_to_right_direction` |
| trigger 통과 시 PARTIAL이면 OCR 0회 | `test_partial_card_is_not_read` |
| 세션 A/B 추적 상태 분리 | `test_sessions_do_not_share_tracking_state` |
| 첫 줄 = title, 줄바꿈 제거 후 spaCy 문장 분리, 순서대로 발행, 필수 필드 | `test_title_split_and_sentences_published_in_order`, `SentenceParsingTests` |
| body 3문장 → MQTT 3건 | `test_same_card_read_once_next_card_is_new` (카드당 3건 × 2장) |
| 제목만 있거나 빈 결과 → sentence 발행 0건 | `test_title_only_or_empty_card_publishes_no_sentence` |
| MQTT 장애 / OCR 예외에도 worker 유지 | `test_mqtt_failure_does_not_stop_tracking`, `test_ocr_failure_does_not_stop_next_card` |
| OpenCV/RapidOCR이 event loop thread 밖에서 실행 | `test_blocking_work_runs_off_event_loop` |
| `en_core_web_sm` 없을 때 sentencizer fallback | `test_missing_model_falls_back`, `test_sentencizer_fallback` |
| 실제 RapidOCR로 렌더링한 카드 읽기, 파일 생성 없음 | `RealRapidOcrTests` |

### 실제 영상 E2E (2026-10-28 수행)
위 sentence payload 예시는 이 E2E에서 실제로 받은 메시지 그대로이다(OCR 오인식 `9,ooo` 포함). PoC 영상(`lab/input_video/*.mp4`, 2560×1392)을 휴대폰 카메라 조건(약 6fps, 가로 1920, JPEG q78)으로 바꿔 실제 서버의 `POST /frame`에 보냈다. 로컬 MQTT broker에서 subscriber로 결과를 받았고, 두 세션씩 동시에 전송했다.

| 세션(영상) | OCR한 카드 | sentence 메시지 | 비고 |
|---|---|---|---|
| white (`input.mp4`) | 8 | 38 | 카드마다 1회, `sentence_index` 순서 일치 |
| offwhite (`input_off-white.mp4`) | 5 | 24 | 〃 |
| black (`input_black.mp4`) | 6 | 27 | 〃 |
| lowres (`input_low-resolution.mp4`) | 6 | 29 | 〃 |

- 전체에서 `message_id` 중복 0건, 카드 중복 OCR 0건, 세션 간 track 섞임 없음, 결과 파일 생성 없음
- 카드당 OCR 시간은 2~7초였다(두 세션 동시 처리 시 RapidOCR lock 대기 포함).

## 6. Reference 파일 삭제 여부

`mobile_ocr_server_with_sentence_parsing.py`는 **삭제해도 된다.** production 코드 어디에서도 import하지 않는다. 파일을 프로젝트 밖으로 옮긴 상태에서 `import main`과 전체 테스트(42개)가 통과하는 것을 확인했다.

PoC에서 옮기지 않은 것: FastAPI app / `/frame` / WebSocket / HTML / TryCloudflare / uvicorn 실행, `server_output/`, `cv2.imwrite`, `write_text`, JSON·TXT 저장.

## 7. 동작이 바뀐 점 (팀 확인 필요)

- **카메라 프레임 경로에서 embedding 생성과 Supabase 저장을 하지 않는다.** 이번 작업 범위에서 Embedding/Gemini/Supabase를 제외했기 때문이다. `db/supabase.py`, `embedding_service.py`, `gemini_service.py` 파일은 그대로 있지만, 카메라 OCR 결과가 더 이상 `ocr_documents`(RAG 검색 대상)에 쌓이지 않는다. 다시 연결하려면 `ocr/+/sentence` 또는 `ocr/text`를 구독하는 쪽에서 저장하는 방식을 권장한다.
- status 토픽은 프레임마다 보내던 `received`, `embedding_completed`, `stored`를 보내지 않는다. 카드 1장마다 `ocr_completed` / `skipped` / `failed`를 보내며 `track_id`, `card_index`가 붙는다.
- 직전 문서와 90% 비슷하면 건너뛰던 텍스트 중복 필터(`DUPLICATE_SIMILARITY`)를 없앴다. 카드 단위 track `processed` 플래그가 그 역할을 대신한다.

## 8. Known limitation

- **black theme 카드 검출 성능은 아직 낮다.** 이번 샘플 영상에서는 6장을 읽었지만, black theme용 검출은 따로 튜닝하지 않았다(작업 범위 밖). 색이 아닌 세로 edge 기반 검출이라 배경과 카드 경계의 대비가 약하면 놓칠 수 있다.
- **빛 반사/glare가 심하면 OCR 정확도가 떨어질 수 있다.** glare 제거와 perspective correction은 하지 않는다. `docs/ocr-tracking-test.md`처럼 반사 위치를 피하도록 `OCR_TRIGGER_RATIO`로 trigger 선을 옮길 수 있다.
- **현재 카메라 페이지 전송 간격(`static/camera/camera.js`의 `CAPTURE_INTERVAL_MS = 2000`, 0.5fps)에서는 추적이 거의 이어지지 않는다.** 2초 사이에 카드가 너무 많이 움직이면 매 프레임 새 track이 되고, 그러면 trigger 통과를 판단할 수 없다. PoC는 약 160ms(약 6fps)로 검증했다. 카메라 코드는 이번 작업에서 수정하지 않았으므로 카메라 담당자와 전송 간격(예: 160~200ms)을 맞춰야 실제 휴대폰에서 동작한다.
- RapidOCR 영어 모델이 단어 사이 공백을 빠뜨리거나(`clearsky`) 숫자 0을 o로 읽는 경우가 있다. 이런 문장은 spaCy 문장 분리 품질에도 영향을 준다.
- 스트리밍을 시작할 때 이미 trigger 선을 지나 있던 카드는 읽지 않는다(선을 "넘는" 순간이 없음).
- `track_id`, `card_index`는 프로세스 메모리 기준이다. 서버를 재시작하거나 10분 넘게 프레임이 없어 세션 상태가 정리되면 1부터 다시 시작하므로, `message_id`는 세션의 한 번의 추적 구간 안에서만 고유하다.
- OCR 큐(세션당 100장)가 가득 차면 카드를 버리고 status `failed`를 보낸다.
- 단일 프로세스(uvicorn worker 1개) 전제다. 추적 상태는 메모리에만 있다.
