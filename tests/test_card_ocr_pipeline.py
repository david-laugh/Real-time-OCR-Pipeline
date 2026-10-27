"""frame_sink 카드 추적 → OCR → MQTT 문장 발행 테스트.

합성 프레임(어두운 배경 위를 가로로 흐르는 흰 카드)으로 실제 OpenCV 검출/추적을 돌리고,
RapidOCR(read_card / run_rapidocr)과 MQTT publish만 mock한다.
"""

import importlib.util
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from services import frame_sink, mqtt_service, ocr_service
from services.frame_sink import OcrSink, crossed_trigger_line, is_fully_visible

W, H = 1280, 720
CARD_W, CARD_TOP, CARD_BOTTOM = 220, 180, 540
STEP = 30
ORCHARD_RAW = (
    "THE ORCHARD IN FEBRUARY\n"
    "Pruning looks like violence to anyone who has\n"
    "not done it. A third of last year's growth comes\n"
    "off, sometimes more, and the tree stands in the\n"
    "cold looking robbed.\n"
    "By August the argument is settled. Light reaches\n"
    "the inner branches."
)
ORCHARD_SENTENCES = [
    "Pruning looks like violence to anyone who has not done it.",
    "A third of last year's growth comes off, sometimes more, and the tree stands in the cold looking robbed.",
    "By August the argument is settled.",
    "Light reaches the inner branches.",
]
THREE_SENTENCES = {
    "title": "Title", "body": "One. Two. Three.", "sentences": ["One.", "Two.", "Three."],
    "raw_text": "Title\nOne. Two. Three.", "confidence": 0.9,
}


def make_frame(card_xs):
    image = np.full((H, W, 3), 40, np.uint8)
    for x in card_xs:
        cv2.rectangle(image, (x, CARD_TOP), (x + CARD_W, CARD_BOTTOM), (235, 235, 235), -1)
    return cv2.imencode(".jpg", image)[1].tobytes()


def conveyor(frames=90, direction=-1, cards=2, gap=700):
    """카드 cards장이 화면 밖에서 들어와 반대편으로 빠져나가는 프레임 목록."""
    starts = [W + 20 + gap * i for i in range(cards)] if direction < 0 else [-CARD_W - 20 - gap * i for i in range(cards)]
    return [make_frame([s + direction * STEP * i for s in starts]) for i in range(frames)]


def sentence_messages(publish, session_id):
    topic = mqtt_service.sentence_topic(session_id)
    return [c.args[1] for c in publish.call_args_list if c.args[0] == topic]


class CardOcrPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def run_frames(self, frames, session_id="s1", read_card=None, publish_error=None):
        sink = OcrSink()
        self.addAsyncCleanup(sink.stop)
        read_card = read_card or (lambda image: dict(THREE_SENTENCES))
        with patch("services.frame_sink.ocr_service.read_card", side_effect=read_card) as read, \
             patch("services.mqtt_service.publish", side_effect=publish_error) as publish:
            for frame in frames:
                await sink.handle(session_id, frame)
            await sink.stop()
        return read, publish

    async def test_same_card_read_once_next_card_is_new(self):
        sink = OcrSink()
        frames = conveyor(direction=-1)
        reads_per_frame = []
        with patch("services.frame_sink.ocr_service.read_card", return_value=dict(THREE_SENTENCES)) as read, \
             patch("services.mqtt_service.publish") as publish:
            for frame in frames:
                result = await sink.handle("s1", frame)
                reads_per_frame.append(result["cards_queued"])
            await sink.stop()

        triggered = [i for i, n in enumerate(reads_per_frame) if n]
        self.assertEqual(sum(reads_per_frame), 2)
        self.assertEqual(read.call_count, 2)
        # 첫 카드가 trigger를 지난 뒤에도 20프레임 이상 화면에 남아 있지만 다시 읽지 않는다
        self.assertGreaterEqual(triggered[1] - triggered[0], 20)
        messages = sentence_messages(publish, "s1")
        self.assertEqual([m["message_id"] for m in messages],
                         ["s1:1:0", "s1:1:1", "s1:1:2", "s1:2:0", "s1:2:1", "s1:2:2"])
        self.assertEqual([m["card_index"] for m in messages], [1, 1, 1, 2, 2, 2])

    async def test_left_to_right_direction(self):
        read, publish = await self.run_frames(conveyor(direction=1))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(sentence_messages(publish, "s1")), 6)

    async def test_partial_card_is_not_read(self):
        # trigger를 오른쪽 끝 근처에 두면 카드가 선을 넘는 순간에는 아직 일부만 보인다 → OCR 안 함
        with patch.object(frame_sink, "TRIGGER_RATIO", 0.95):
            read, publish = await self.run_frames(conveyor(direction=-1, cards=1, frames=60))
        self.assertEqual(read.call_count, 0)
        self.assertEqual(sentence_messages(publish, "s1"), [])

    async def test_sessions_do_not_share_tracking_state(self):
        sink = OcrSink()
        frames_a, frames_b = conveyor(direction=-1, cards=1, frames=60), conveyor(direction=1, cards=1, frames=60)
        with patch("services.frame_sink.ocr_service.read_card", return_value=dict(THREE_SENTENCES)) as read, \
             patch("services.mqtt_service.publish") as publish:
            for frame_a, frame_b in zip(frames_a, frames_b):
                await sink.handle("A", frame_a)
                await sink.handle("B", frame_b)
            await sink.stop()
        self.assertEqual(read.call_count, 2)
        self.assertEqual([m["message_id"] for m in sentence_messages(publish, "A")], ["A:1:0", "A:1:1", "A:1:2"])
        self.assertEqual([m["message_id"] for m in sentence_messages(publish, "B")], ["B:1:0", "B:1:1", "B:1:2"])

    async def test_title_split_and_sentences_published_in_order(self):
        with patch("services.ocr_service.run_rapidocr", return_value=(ORCHARD_RAW, 0.97)):
            read, publish = await self.run_frames(
                conveyor(direction=-1, cards=1, frames=60), read_card=ocr_service.read_card,
            )
        messages = sentence_messages(publish, "s1")
        self.assertEqual([m["sentence"] for m in messages], ORCHARD_SENTENCES)
        self.assertEqual([m["sentence_index"] for m in messages], [0, 1, 2, 3])
        for m in messages:
            self.assertEqual(m["type"], "ocr_sentence")
            self.assertEqual(m["session_id"], "s1")
            self.assertEqual(m["track_id"], 1)
            self.assertEqual(m["title"], "THE ORCHARD IN FEBRUARY")
            self.assertEqual(m["ocr_confidence"], 0.97)
            self.assertIsInstance(m["captured_at"], float)
        # 대시보드 계약(ocr/text)은 카드당 1건, 첫 줄이 제목
        documents = [c.args[1] for c in publish.call_args_list if c.args[0].endswith("text")]
        self.assertEqual(len(documents), 1)
        self.assertTrue(documents[0]["text"].startswith("THE ORCHARD IN FEBRUARY\nPruning looks"))

    async def test_title_only_or_empty_card_publishes_no_sentence(self):
        for raw in ("ONLY A TITLE", ""):
            with patch("services.ocr_service.run_rapidocr", return_value=(raw, None)):
                _, publish = await self.run_frames(
                    conveyor(direction=-1, cards=1, frames=60), read_card=ocr_service.read_card,
                )
            self.assertEqual(sentence_messages(publish, "s1"), [])
            stages = [c.args[1]["stage"] for c in publish.call_args_list
                      if c.args[0] == mqtt_service.status_topic("s1")]
            self.assertEqual(stages, ["skipped"])

    async def test_mqtt_failure_does_not_stop_tracking(self):
        read, publish = await self.run_frames(conveyor(direction=-1), publish_error=RuntimeError("broker down"))
        self.assertEqual(read.call_count, 2)
        self.assertGreater(publish.call_count, 0)

    async def test_ocr_failure_does_not_stop_next_card(self):
        calls = []

        def flaky(image):
            calls.append(image)
            if len(calls) == 1:
                raise RuntimeError("ocr crashed")
            return dict(THREE_SENTENCES)

        read, publish = await self.run_frames(conveyor(direction=-1), read_card=flaky)
        self.assertEqual(read.call_count, 2)
        self.assertEqual([m["message_id"] for m in sentence_messages(publish, "s1")], ["s1:2:0", "s1:2:1", "s1:2:2"])

    async def test_blocking_work_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        threads = []
        original = frame_sink.process_frame

        def record_frame(state, jpeg_bytes):
            threads.append(threading.get_ident())
            return original(state, jpeg_bytes)

        def record_ocr(image):
            threads.append(threading.get_ident())
            return dict(THREE_SENTENCES)

        with patch("services.frame_sink.process_frame", side_effect=record_frame):
            await self.run_frames(conveyor(direction=-1, cards=1, frames=60), read_card=record_ocr)
        self.assertEqual(len(threads), 61)
        self.assertNotIn(loop_thread, threads)

    async def test_invalid_jpeg_raises_without_breaking_session(self):
        sink = OcrSink()
        self.addAsyncCleanup(sink.stop)
        with self.assertRaises(ValueError):
            await sink.handle("s1", b"\xff\xd8\xffbroken")
        result = await sink.handle("s1", make_frame([500]))
        self.assertEqual(result["cards_queued"], 0)


class TriggerRuleTests(unittest.TestCase):
    def test_full_visibility(self):
        self.assertTrue(is_fully_visible((100, 100, 320, 460), W, H))
        self.assertFalse(is_fully_visible((0, 100, 220, 460), W, H))
        self.assertFalse(is_fully_visible((1100, 100, W, 460), W, H))

    def test_trigger_crossing_both_directions(self):
        self.assertTrue(crossed_trigger_line(720, 690, 704))
        self.assertTrue(crossed_trigger_line(690, 720, 704))
        self.assertFalse(crossed_trigger_line(None, 690, 704))
        self.assertFalse(crossed_trigger_line(720, 710, 704))   # 근처지만 넘지 않음
        self.assertFalse(crossed_trigger_line(690, 690, 704))


class SentenceParsingTests(unittest.TestCase):
    def test_title_and_sentences(self):
        parsed = ocr_service.parse_ocr_document(ORCHARD_RAW)
        self.assertEqual(parsed["title"], "THE ORCHARD IN FEBRUARY")
        self.assertEqual(parsed["sentences"], ORCHARD_SENTENCES)
        self.assertNotIn("\n", parsed["body"])

    def test_sentencizer_fallback(self):
        import spacy

        blank = spacy.blank("en")
        blank.add_pipe("sentencizer")
        with patch.object(ocr_service, "_nlp", blank):
            self.assertEqual(ocr_service.parse_ocr_document(ORCHARD_RAW)["sentences"], ORCHARD_SENTENCES)

    def test_missing_model_falls_back(self):
        with patch.object(ocr_service, "SPACY_MODEL", "no_such_model"):
            nlp = ocr_service._build_nlp()
        self.assertIn("sentencizer", nlp.pipe_names)

    def test_empty_and_title_only(self):
        self.assertEqual(ocr_service.parse_ocr_document(""), {"title": "", "body": "", "sentences": []})
        self.assertEqual(ocr_service.parse_ocr_document("\n  TITLE  \n\n")["sentences"], [])


@unittest.skipUnless(
    importlib.util.find_spec("rapidocr") or importlib.util.find_spec("rapidocr_onnxruntime"), "RapidOCR not installed",
)
class RealRapidOcrTests(unittest.TestCase):
    def test_rendered_card_without_writing_files(self):
        lines = ORCHARD_RAW.split("\n")
        card = np.full((560, 900, 3), 245, np.uint8)
        for i, line in enumerate(lines):
            cv2.putText(card, line, (30, 60 + i * 70), cv2.FONT_HERSHEY_SIMPLEX, 1.2 if i == 0 else 1.0,
                        (20, 20, 20), 2, cv2.LINE_AA)
        with patch("cv2.imwrite", side_effect=AssertionError("file written")), \
             patch.object(Path, "write_text", side_effect=AssertionError("file written")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("file written")):
            document = ocr_service.read_card(card)
        self.assertEqual(document["title"], "THE ORCHARD IN FEBRUARY")
        self.assertEqual(document["sentences"], ORCHARD_SENTENCES)


if __name__ == "__main__":
    unittest.main()
