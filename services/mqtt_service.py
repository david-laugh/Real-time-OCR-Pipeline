"""OCR 파이프라인 상태를 MQTT로 발행 (paho-mqtt). MQTT 라이브러리는 이 파일에서만 import 한다.

환경변수
  MQTT_HOST             기본 localhost
  MQTT_PORT             기본 1883
  MQTT_USERNAME         선택
  MQTT_PASSWORD         선택
  MQTT_TLS              "1"이면 TLS 사용 (클라우드 broker, 보통 포트 8883)
  MQTT_TOPIC_PREFIX     기본 ocr

토픽: <prefix>/<session_id>/status   예) ocr/abc123/status
      대시보드는 ocr/+/status 를 구독하면 모든 세션을 받는다.
payload: UTF-8 JSON (frame_sink.OcrSink._notify 참고), QoS 1, retain 없음

연결/재연결은 paho 백그라운드 스레드(loop_start)가 처리하고, publish()는
전송 큐에 넣고 바로 반환하므로 이벤트 루프를 막지 않는다.
broker가 잠시 끊겨도 QoS 1 메시지는 큐에 남았다가 재연결 후 전송된다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "ocr")
QOS = 1

_client: Optional[mqtt.Client] = None
_lock = threading.Lock()


def get_client() -> mqtt.Client:
    # import 시점이 아니라 첫 발행 때 연결한다 (broker 없이도 서버는 뜨도록)
    global _client
    with _lock:
        if _client is None:
            host = os.getenv("MQTT_HOST", "localhost")
            port = int(os.getenv("MQTT_PORT", "1883"))
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            username = os.getenv("MQTT_USERNAME")
            if username:
                client.username_pw_set(username, os.getenv("MQTT_PASSWORD"))
            if os.getenv("MQTT_TLS") == "1":
                client.tls_set()
            client.on_connect = _on_connect
            client.on_disconnect = _on_disconnect
            client.connect_async(host, port)
            client.loop_start()
            _client = client
        return _client


def status_topic(session_id: str) -> str:
    return f"{TOPIC_PREFIX}/{session_id}/status"


def publish(topic: str, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False)
    info = get_client().publish(topic, body, qos=QOS)
    if info.rc == mqtt.MQTT_ERR_NO_CONN:
        # QoS 1은 paho가 큐에 보관했다가 (재)연결되면 보낸다 → 실패 아님
        logger.debug("MQTT not connected yet; queued message for %s", topic)
    elif info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish to {topic} failed: {mqtt.error_string(info.rc)}")


def _on_connect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code.is_failure:
        logger.error("MQTT connect failed: %s", reason_code)
    else:
        logger.info("MQTT connected")


def _on_disconnect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code != 0:
        logger.warning("MQTT disconnected (%s); paho will reconnect", reason_code)
