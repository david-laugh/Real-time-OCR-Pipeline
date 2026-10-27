"""카드 이미지 → 영어 원문 추출 (RapidOCR) → 제목 + 문장 분리 (spaCy).

read_card()는 블로킹 함수다. async 코드에서는 asyncio.to_thread()로 호출할 것.
결과는 메모리로만 돌려주며 이미지/텍스트/JSON 파일을 만들지 않는다.

OCR 줄바꿈은 문장 경계가 아니라 화면 폭에 따른 줄 넘김이므로
  1. RapidOCR 줄을 위→아래 순으로 정렬하고, 첫 번째 비어 있지 않은 줄을 제목으로 쓴다
  2. 나머지 줄은 공백으로 이어 붙여 본문 하나로 만든다
  3. 본문을 spaCy sentence boundary detection으로 문장 단위로 다시 나눈다 (제목은 문장에 넣지 않음)

RapidOCR 엔진과 spaCy 파이프라인은 프로세스에서 한 번만 만들어 재사용한다.
en_core_web_sm이 설치되어 있지 않으면 spacy.blank("en") + sentencizer로 대신한다.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

OCR_TARGET_HEIGHT = 1400    # 카드 crop이 이보다 작으면 이 높이로 확대
OCR_INSET_RATIO = 0.012     # 카드 외곽선이 글자로 읽히지 않도록 가장자리를 이만큼 잘라낸다
SPACY_MODEL = "en_core_web_sm"

_engine: Any = None
# 엔진 생성과 추론을 한 번에 하나씩 (여러 세션의 OCR worker가 동시에 호출해도 안전하도록)
_engine_lock = threading.Lock()
_nlp: Any = None
_nlp_lock = threading.Lock()


def read_card(image: np.ndarray) -> Dict[str, Any]:
    """카드 crop(BGR) → {"title", "body", "sentences", "raw_text", "confidence"}."""
    raw_text, confidence = run_rapidocr(image)
    return {**parse_ocr_document(raw_text), "raw_text": raw_text, "confidence": confidence}


def extract_text(jpeg_bytes: bytes) -> Dict[str, str]:
    """JPEG 한 장 전체 → {"title", "text"}. 카드 추적 없이 바로 읽을 때 쓰는 기존 인터페이스."""
    frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Failed to decode JPEG")
    document = read_card(frame)
    return {"title": document["title"], "text": document["body"]}


def warmup() -> None:
    """서버 기동 시 백그라운드에서 모델을 미리 올린다. 실패해도 첫 카드에서 다시 시도한다."""
    try:
        with _engine_lock:
            _get_engine()
        _get_nlp()
    except Exception:
        logger.exception("OCR warmup failed; models will be loaded on the first card")


# ---------------------------------------------------------------- RapidOCR

def prepare_ocr_image(image: np.ndarray, target_height: int = OCR_TARGET_HEIGHT) -> np.ndarray:
    height, width = image.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError("Empty OCR image")

    inset_x = max(1, int(width * OCR_INSET_RATIO))
    inset_y = max(1, int(height * OCR_INSET_RATIO))
    if width > inset_x * 2 and height > inset_y * 2:
        image = image[inset_y:height - inset_y, inset_x:width - inset_x]

    height, width = image.shape[:2]
    if height >= target_height:
        return image
    scale = target_height / height
    return cv2.resize(image, (int(round(width * scale)), target_height), interpolation=cv2.INTER_CUBIC)


def run_rapidocr(image: np.ndarray) -> Tuple[str, Optional[float]]:
    """→ (위→아래 순으로 정렬한 OCR 줄들을 \\n으로 이은 원문, 평균 confidence 또는 None)."""
    prepared = prepare_ocr_image(image)
    with _engine_lock:
        result = _get_engine()(prepared)

    items = _result_items(result)
    text = normalize_ocr_text("\n".join(item[2] for item in items))
    scores = [item[3] for item in items if item[3] is not None]
    return text, (float(np.mean(scores)) if scores else None)


def _get_engine() -> Any:
    # _engine_lock을 잡은 상태에서 호출한다
    global _engine
    if _engine is None:
        try:
            from rapidocr import RapidOCR               # rapidocr >= 2
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR   # rapidocr-onnxruntime 1.x
        _engine = RapidOCR()
        logger.info("RapidOCR engine loaded")
    return _engine


def _result_items(result: Any) -> List[Tuple[float, float, str, Optional[float]]]:
    """RapidOCR 결과 → (top, left, text, score) 목록. 버전마다 결과 형태가 달라 둘 다 처리한다."""
    if result is None:
        return []
    if hasattr(result, "txts"):
        # rapidocr >= 2: RapidOCROutput(boxes, txts, scores). 글자가 없으면 각 필드가 None
        texts = _as_list(result.txts)
        boxes = _as_list(result.boxes)
        scores = _as_list(result.scores)
        rows = [
            (boxes[i] if i < len(boxes) else None, texts[i], scores[i] if i < len(scores) else None)
            for i in range(len(texts))
        ]
    else:
        # rapidocr-onnxruntime 1.x: ([[box, text, score], ...] 또는 None, elapse)
        raw = result[0] if isinstance(result, tuple) else result
        rows = [
            (item[0], item[1], item[2] if len(item) >= 3 else None)
            for item in raw or []
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]

    items = []
    for index, (box, text, score) in enumerate(rows):
        text = str(text).strip()
        if not text:
            continue
        if box is not None:
            points = np.asarray(box, dtype=float).reshape(-1, 2)
            top, left = float(points[:, 1].min()), float(points[:, 0].min())
        else:
            top, left = float(index), 0.0
        items.append((top, left, text, _to_float(score)))
    items.sort(key=lambda item: (item[0], item[1]))
    return items


def _as_list(value: Any) -> list:
    return [] if value is None else list(value)


def _to_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 제목 / 문장 분리

def normalize_ocr_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_ocr_document(ocr_text: str) -> Dict[str, Any]:
    """OCR 원문(줄 단위) → {"title", "body", "sentences"}."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in ocr_text.splitlines() if line.strip()]
    if not lines:
        return {"title": "", "body": "", "sentences": []}

    title = lines[0]
    body = " ".join(lines[1:]).strip()
    if not body:
        return {"title": title, "body": "", "sentences": []}

    nlp = _get_nlp()
    with _nlp_lock:
        doc = nlp(body)
    sentences = [sentence.text.strip() for sentence in doc.sents if sentence.text.strip()]
    return {"title": title, "body": body, "sentences": sentences}


def _get_nlp() -> Any:
    global _nlp
    with _nlp_lock:
        if _nlp is None:
            _nlp = _build_nlp()
        return _nlp


def _build_nlp() -> Any:
    import spacy

    try:
        # 문장 경계에는 parser만 필요하다
        nlp = spacy.load(SPACY_MODEL, disable=["ner", "lemmatizer", "attribute_ruler", "tagger"])
        logger.info("Sentence segmentation: %s parser", SPACY_MODEL)
    except Exception as exc:
        logger.warning("%s unavailable (%s); falling back to spaCy sentencizer", SPACY_MODEL, exc)
        nlp = spacy.blank("en")
        nlp.add_pipe("sentencizer")
    return nlp
