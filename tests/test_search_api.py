"""Tests for POST /api/rag/search and rag_service (no network: Gemini/Supabase are faked).

Run from the repo root:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from google.genai import errors

import main
from services import gemini_service, rag_service

ERROR_KEYS = {"answer", "sources", "error"}


def api_error(code: int) -> errors.APIError:
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "message": "secret quota details", "status": "X"}})


def row(doc_id: str, title: str, text: str, similarity: float) -> dict:
    return {"id": doc_id, "session_id": "test", "title": title, "text": text, "similarity": similarity}


CARD = (
    "READING IN TRANSLATION\nA translator makes ten thousand small betrayals in the service "
    "of one large loyalty, and the reader never learns which."
)


class SearchApiFormatTest(unittest.TestCase):
    """Every response must have answer + sources so the pec dashboard can read data.answer."""

    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def post(self, body):
        return self.client.post("/api/rag/search", json=body)

    def assert_error(self, res, status: int, code: str) -> None:
        self.assertEqual(res.status_code, status)
        data = res.json()
        self.assertEqual(set(data), ERROR_KEYS)
        self.assertEqual(data["sources"], [])
        self.assertEqual(data["error"], code)
        self.assertTrue(data["answer"])
        self.assertNotIn("secret", data["answer"])

    def test_success_passes_through(self) -> None:
        result = {"answer": "ok [1]", "sources": [{"doc_id": "a", "title": "t", "preview": "p", "score": 0.9}]}
        with mock.patch.object(rag_service, "search", mock.AsyncMock(return_value=result)) as search:
            res = self.post({"query": "q", "limit": 3, "session_id": "seed"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), result)
        search.assert_awaited_once_with("q", limit=3, session_id="seed")

    def test_invalid_session_id_is_400(self) -> None:
        self.assert_error(self.post({"query": "q", "session_id": "bad id!"}), 400, "invalid_request")

    def test_validation_errors_are_400(self) -> None:
        for body in ({"query": "q", "limit": 0}, {"limit": 3}, {"query": "x" * 1001}):
            with self.subTest(body=body):
                self.assert_error(self.post(body), 400, "invalid_request")
        res = self.client.post("/api/rag/search", content=b"not json", headers={"Content-Type": "application/json"})
        self.assert_error(res, 400, "invalid_request")

    def test_rate_limited_is_429(self) -> None:
        with mock.patch.object(rag_service, "search", mock.AsyncMock(side_effect=rag_service.RateLimitedError())):
            self.assert_error(self.post({"query": "q"}), 429, "rate_limited")

    def test_unavailable_is_503(self) -> None:
        with mock.patch.object(rag_service, "search", mock.AsyncMock(side_effect=rag_service.UnavailableError())):
            self.assert_error(self.post({"query": "q"}), 503, "unavailable")

    def test_other_failure_is_502_without_raw_error(self) -> None:
        boom = RuntimeError("secret key and quota details")
        with mock.patch.object(rag_service, "search", mock.AsyncMock(side_effect=boom)), \
                self.assertLogs("api.search", "ERROR"):
            self.assert_error(self.post({"query": "q"}), 502, "search_failed")

    def test_chat_page(self) -> None:
        res = self.client.get("/chat")
        self.assertEqual(res.status_code, 200)
        self.assertIn("<rag-chat", res.text)

    def test_fts_search_path_stays_with_routes(self) -> None:
        # POST /api/search is the FTS5 search in api/routes.py (dashboard.html); RAG must not shadow it
        owners = {
            (route.endpoint.__module__, path)
            for route in main.app.routes
            for path in [getattr(route, "path", "")]
            if path in ("/api/search", "/api/rag/search")
        }
        self.assertEqual(owners, {("api.routes", "/api/search"), ("api.search", "/api/rag/search")})

    def test_other_routes_keep_default_validation(self) -> None:
        # SearchRoute must not change other routers (e.g. /frame still returns 422 {detail})
        res = self.client.post("/frame", data={})
        self.assertEqual(res.status_code, 422)
        self.assertIn("detail", res.json())


class RagServiceTest(unittest.TestCase):
    def run_search(self, rows, generate=None, embed=None):
        patches = [
            mock.patch.object(gemini_service, "embed_text", embed or mock.Mock(return_value=[0.0] * 768)),
            mock.patch.object(rag_service.supabase, "match_ocr_documents", mock.Mock(return_value=rows)),
            mock.patch.object(gemini_service, "generate_text", generate or mock.Mock(return_value="answer")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return asyncio.run(rag_service.search("question", limit=5))

    def test_dedupe_keeps_longest_and_titles_fallback(self) -> None:
        rows = [
            row("a", "{G IN TRANSLATION", CARD[8:], 0.73),
            row("b", "214", "214\n\n22 24\n\n" + CARD + " Extra words here.", 0.71),
            row("c", "4", "4", 0.6),  # too short -> filtered
        ]
        sources = self.run_search(rows)["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["doc_id"], "b")
        self.assertEqual(sources[0]["title"], "READING IN TRANSLATION")
        self.assertEqual(sources[0]["score"], 0.73)

    def test_no_rows_skips_gemini(self) -> None:
        generate = mock.Mock()
        result = self.run_search([], generate=generate)
        self.assertEqual(result, {"answer": rag_service.NO_RESULT_ANSWER, "sources": []})
        generate.assert_not_called()

    def test_rate_limit_retries_fallback_once(self) -> None:
        generate = mock.Mock(side_effect=[api_error(429), "from fallback"])
        with mock.patch.object(gemini_service, "CHAT_MODEL", "primary"), \
                mock.patch.object(gemini_service, "CHAT_FALLBACK_MODEL", "fallback"), \
                self.assertLogs("services.rag_service", "WARNING"):
            result = self.run_search([row("a", "T", CARD, 0.7)], generate=generate)
        self.assertEqual(result["answer"], "from fallback")
        self.assertEqual([c.args[2] for c in generate.call_args_list], ["primary", "fallback"])

    def test_rate_limit_on_both_models_raises(self) -> None:
        generate = mock.Mock(side_effect=[api_error(429), api_error(429)])
        with self.assertRaises(rag_service.RateLimitedError), self.assertLogs("services.rag_service", "WARNING"):
            self.run_search([row("a", "T", CARD, 0.7)], generate=generate)

    def test_gemini_503_raises_unavailable(self) -> None:
        for target in ("embed", "generate"):
            with self.subTest(target=target), self.assertRaises(rag_service.UnavailableError), \
                    self.assertLogs("services.rag_service", "WARNING"):
                failing = mock.Mock(side_effect=api_error(503))
                kwargs = {target: failing}
                self.run_search([row("a", "T", CARD, 0.7)], **kwargs)

    def test_quality_filter_keeps_numeric_heavy_cards(self) -> None:
        self.assertFalse(rag_service.is_quality_text("4uty"))
        self.assertFalse(rag_service.is_quality_text("12 34 56 78 90 12 34 56 78 90 12 34 56 78 90 1"))
        self.assertTrue(rag_service.is_quality_text(
            "06:00 -2 C, frost on the north field, visibility approx. 300 m. Barometer 1013 hPa"
        ))


if __name__ == "__main__":
    unittest.main()
