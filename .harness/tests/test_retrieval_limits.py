import os
import unittest
from types import SimpleNamespace
from unittest import mock

import rag_backend
import retrieval_limits as limits


class RetrievalLimitTests(unittest.TestCase):
    def test_pair_budget_counts_query_and_multibyte_documents(self):
        original = ["code " * 1000, "你好🌍" * 1000]
        fitted, report = limits.fit_rerank_documents("query " * 10, original, {"max_input_tokens": 512})
        self.assertEqual(report["clipped_documents"], 2)
        self.assertEqual(report["mode"], "conservative_utf8_estimate")
        for value in fitted:
            self.assertLessEqual(len(value.encode()) + 60 + 16, 512)
        self.assertEqual(original[0], "code " * 1000)

    def test_query_overflow_is_explicit(self):
        with self.assertRaises(limits.RerankInputBudgetExceeded):
            limits.fit_rerank_documents("x" * 512, ["text"], {"max_input_tokens": 512})

    def test_matching_local_tokenizer_counts_full_pairs(self):
        # Contract mock independent of optional tokenizers installation.
        tokenizer = SimpleNamespace(encode=lambda query, doc, add_special_tokens: SimpleNamespace(ids=list(query + doc) + [0, 0, 0]))
        with mock.patch.object(limits, "_load_tokenizer", return_value=tokenizer), \
             mock.patch.object(limits.Path, "stat", return_value=SimpleNamespace(st_size=100, st_mtime_ns=1)), \
             mock.patch.object(limits.Path, "is_file", return_value=True):
            documents, report = limits.fit_rerank_documents("query", ["x" * 100], {"max_input_tokens": 32, "tokenizer_path": "/tokenizer.json"})
        self.assertEqual(len(documents[0]), 24)
        self.assertEqual(report["mode"], "configured_tokenizer_pair")

    def test_invalid_tokenizer_never_silently_switches_to_estimate(self):
        with self.assertRaises(OSError):
            limits.fit_rerank_documents("query", ["doc"], {"tokenizer_path": "/missing/tokenizer.json"})

    def test_reranking_preserves_full_hits_and_exposes_clipping(self):
        hits = [{"id": "a", "preview": "hello " * 1000, "score": 1}]
        with mock.patch.dict(os.environ, {"AWOKI_RERANK_ENABLED": "1", "AWOKI_RERANK_URL": "http://localhost/rerank"}), \
             mock.patch.object(rag_backend, "_remote_rerank_scores", return_value=[(0, 0.5)]) as remote:
            result = rag_backend.rerank_hits("hello", hits)
        self.assertLess(len(remote.call_args.args[1][0]), 512)
        self.assertEqual(result[0]["preview"], hits[0]["preview"])
        self.assertEqual(result[0]["rerank_input_budget"]["clipped_documents"], 1)

    def test_oversized_query_fallback_never_calls_provider(self):
        hits = [{"id": "a", "preview": "doc", "score": 1}]
        with mock.patch.dict(os.environ, {"AWOKI_RERANK_ENABLED": "1", "AWOKI_RERANK_FAIL_MODE": "fallback"}), \
             mock.patch.object(rag_backend, "_remote_rerank_scores") as remote:
            result = rag_backend.rerank_hits("x" * 2000, hits)
        remote.assert_not_called()
        self.assertEqual(result[0]["rerank_failure"]["failure_category"], "input_budget_exceeded")
        self.assertEqual(result[0]["score"], 1)

    def test_rate_limit_cooldown_expires_is_bounded_and_secret_free(self):
        exc = RuntimeError("provider body contains private data")
        exc.response = SimpleNamespace(status_code=429, headers={"retry-after": "10000"})
        key = limits.provider_key("https://endpoint.invalid", "model", "private-key")
        with mock.patch.object(limits.time, "monotonic", return_value=10):
            limits.note_rate_limit(key, exc)
            self.assertEqual(limits.error_metadata(exc)["retry_after_seconds"], 60)
            with self.assertRaises(limits.ProviderCoolingDown):
                limits.check_cooldown(key)
            limits.check_cooldown(limits.provider_key("https://endpoint.invalid", "model", "rotated-key"))
        with mock.patch.object(limits.time, "monotonic", return_value=71):
            limits.check_cooldown(key)
        self.assertNotIn("private", str(limits.error_metadata(exc)))

    def test_http_429_causes_one_call_then_fallback_without_retry(self):
        exc = RuntimeError("sensitive provider details")
        exc.response = SimpleNamespace(status_code=429, headers={"retry-after": "10"})
        httpx = mock.Mock()
        httpx.post.side_effect = exc
        with mock.patch.dict(os.environ, {"AWOKI_RERANK_ENABLED": "1", "AWOKI_RERANK_FAIL_MODE": "fallback", "AWOKI_RERANK_URL": "http://rate-limit-test/rerank"}), \
             mock.patch.dict("sys.modules", {"httpx": httpx}):
            first = rag_backend.rerank_hits("query", [{"id": "a", "preview": "doc"}])
            second = rag_backend.rerank_hits("query", [{"id": "a", "preview": "doc"}])
        self.assertEqual(httpx.post.call_count, 1)
        self.assertEqual(first[0]["rerank_failure"]["failure_category"], "rate_limited")
        self.assertEqual(second[0]["rerank_failure"]["failure_category"], "rate_limited")
        self.assertNotIn("sensitive provider", str(first))
