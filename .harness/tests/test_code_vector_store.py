from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from code_search import vector_store


class FakeQdrant:
    def __init__(self, points=None):
        self.points = dict(points or {})
        self.payload_updates = []
        self.upserts = []
        self.upsert_calls = []
        self.retrieve_calls = []
        self.deletes = []

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        self.retrieve_calls.append(list(ids))
        return [self.points[value] for value in ids if value in self.points]

    def collection_exists(self, collection_name):
        return True

    def set_payload(self, *, collection_name, payload, points, wait):
        self.payload_updates.append((collection_name, payload, list(points)))
        for point_id in points:
            point = self.points.get(point_id)
            if point is not None:
                point.payload = payload

    def upsert(self, *, collection_name, points, wait):
        self.upsert_calls.append(list(points))
        self.upserts.extend(points)

    def delete(self, *, collection_name, points_selector, wait):
        ids = list(points_selector.points)
        self.deletes.extend(ids)
        for point_id in ids:
            self.points.pop(point_id, None)


def row(*, key="embed-key", path="src/a.py", chunk="chunk-a", repo="alpha:repo"):
    return {
        "embedding_key": key,
        "content_hash": "content-hash",
        "text": "def guard(): return True",
        "repo_id": repo,
        "path": path,
        "chunk_id": chunk,
        "symbol_id": "symbol-a",
        "symbol_name": "guard",
        "qualified_name": "src.a.guard",
        "symbol_kind": "function",
        "language": "python",
        "start_line": 1,
        "end_line": 1,
        "commit_sha": "abc",
        "dirty": False,
    }


class CodeVectorStoreTests(unittest.TestCase):
    def existing_point(self, *, project_id="alpha", branch_key="branch:main"):
        source = row()
        membership = vector_store._membership_from_row(project_id, branch_key, source)
        payload = vector_store._normalize_payload(
            str(source["embedding_key"]), str(source["content_hash"]), [membership]
        )
        point_id = vector_store._point_id(str(source["embedding_key"]))
        return point_id, SimpleNamespace(id=point_id, payload=payload)

    def test_interactive_vector_search_uses_short_qdrant_timeout(self):
        with mock.patch("rag_backend.qdrant_client", return_value=(None, "down")) as client:
            result = vector_store.search_with_status(
                "decision handler", project_id="alpha", branch_key="branch:main", limit=10
            )
        client.assert_called_once_with(timeout=2.0)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["hits"], [])

    def test_new_vector_is_embedded_from_content_only(self):
        client = FakeQdrant()
        source = row(path="moved/location.py")
        captured = []
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value={"provider": "hash"}), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=lambda texts, is_query=False: captured.append(list(texts)) or [[0.1, 0.2]]):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=[source]
            )
        self.assertEqual(result["new_vectors"], 1)
        self.assertEqual(captured, [[source["text"]]])
        self.assertNotIn(source["path"], captured[0][0])

    def test_existing_content_hash_reuses_vector_for_another_project(self):
        point_id, point = self.existing_point()
        client = FakeQdrant({point_id: point})
        beta_row = row(path="src/shared.py", chunk="chunk-beta", repo="beta:repo")
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embed_texts", side_effect=AssertionError("unchanged content must not be embedded")), \
             mock.patch("rag_backend.embedding_profile", return_value={"deployment_identity": "fixture"}):
            result = vector_store.sync_branch_memberships(
                project_id="beta",
                branch_key="branch:main",
                old_rows=[],
                new_rows=[beta_row],
            )
        self.assertEqual(result["new_vectors"], 0)
        self.assertEqual(result["reused_vectors"], 1)
        self.assertEqual(len(client.payload_updates), 1)
        payload = client.payload_updates[0][1]
        self.assertEqual(payload["project_ids"], ["alpha", "beta"])
        self.assertEqual(len(payload["memberships"]), 2)

    def test_last_membership_removal_deletes_vector_point(self):
        point_id, point = self.existing_point()
        client = FakeQdrant({point_id: point})
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value={"deployment_identity": "fixture"}):
            result = vector_store.sync_branch_memberships(
                project_id="alpha",
                branch_key="branch:main",
                old_rows=[row()],
                new_rows=[],
            )
        self.assertEqual(result["removed_memberships"], 1)
        self.assertEqual(result["deleted_points"], 1)
        self.assertEqual(client.deletes, [point_id])

    def test_existing_point_retrieval_is_batched(self):
        source_rows = [row(key=f"key-{index}", chunk=f"chunk-{index}") for index in range(257)]
        points = {}
        for source in source_rows:
            membership = vector_store._membership_from_row("alpha", "branch:main", source)
            point_id = vector_store._point_id(str(source["embedding_key"]))
            points[point_id] = SimpleNamespace(
                id=point_id,
                payload=vector_store._normalize_payload(
                    str(source["embedding_key"]), str(source["content_hash"]), [membership]
                ),
            )
        client = FakeQdrant(points)
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embed_texts", side_effect=AssertionError("vectors must be reused")), \
             mock.patch("rag_backend.embedding_profile", return_value={"deployment_identity": "fixture"}):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=source_rows, new_rows=source_rows
            )
        self.assertEqual(result["status"], "indexed")
        self.assertEqual([len(batch) for batch in client.retrieve_calls], [256, 1])
        self.assertEqual(client.payload_updates, [])
        self.assertEqual(result["operations"]["payload_updates"], 0)

    def test_failed_embedding_batch_preserves_partial_progress(self):
        source_rows = [row(key=f"key-{index}", chunk=f"chunk-{index}") for index in range(3)]
        client = FakeQdrant()
        calls = 0

        def embed(texts, *, is_query=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [[0.1, 0.2] for _ in texts]
            raise TimeoutError("fixture embedding timeout")

        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value={"provider": "hash", "batch_size": 2, "timeout_seconds": 60}), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["phase"], "embedding")
        self.assertEqual(result["new_vectors"], 2)
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(result["batches_total"], 2)
        self.assertEqual(result["failing_batch"], 2)
        self.assertEqual(result["vectors_remaining"], 1)
        self.assertIn("fixture embedding timeout", result["reason"])

    def test_transient_embedding_timeout_retries_inside_worker_at_minimum_batch_then_succeeds(self):
        source_rows = [row(key="key-a", chunk="chunk-a")]
        client = FakeQdrant()
        calls = []

        def embed(texts, *, is_query=False):
            calls.append(len(texts))
            if len(calls) == 1:
                raise TimeoutError("temporary embedding timeout")
            return [[0.1, 0.2] for _ in texts]

        profile = {
            "provider": "openai", "batch_size": 1, "worker_max_retries": 2,
            "retry_backoff_seconds": 0, "adaptive_min_batch_size": 1,
        }
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value=profile), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "indexed", result)
        self.assertEqual(calls, [1, 1])
        self.assertEqual(result["new_vectors"], 1)
        self.assertEqual(result["embedding_worker_retry_attempts"], 1)

    def test_repeated_transient_timeout_adaptively_splits_request_batch(self):
        source_rows = [row(key=f"key-{index}", chunk=f"chunk-{index}") for index in range(4)]
        client = FakeQdrant()
        calls = []

        def embed(texts, *, is_query=False):
            calls.append(len(texts))
            if len(texts) > 1:
                raise TimeoutError("large request timed out")
            return [[0.1, 0.2]]

        profile = {
            "provider": "openai", "batch_size": 4, "worker_max_retries": 0,
            "retry_backoff_seconds": 0, "adaptive_min_batch_size": 1,
        }
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value=profile), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "indexed", result)
        self.assertEqual(calls, [4, 2, 1, 1, 2, 1, 1])
        self.assertEqual(result["new_vectors"], 4)
        self.assertEqual(result["embedding_adaptive_splits"], 3)
        self.assertEqual([len(batch) for batch in client.upsert_calls], [1, 1, 1, 1])

    def test_rate_limit_retries_but_does_not_adaptively_split(self):
        class RateLimited(RuntimeError):
            status_code = 429

        source_rows = [row(key="key-a", chunk="chunk-a"), row(key="key-b", chunk="chunk-b")]
        client = FakeQdrant()
        calls = []

        def embed(texts, *, is_query=False):
            calls.append(len(texts))
            raise RateLimited("too many requests")

        profile = {
            "provider": "openai", "batch_size": 2, "worker_max_retries": 1,
            "retry_backoff_seconds": 0, "adaptive_min_batch_size": 1,
        }
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value=profile), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(calls, [2, 2])
        self.assertEqual(result["embedding_failure_kind"], "rate_limit")
        self.assertTrue(result["automatic_retries_exhausted"])
        self.assertEqual(result["embedding_adaptive_splits"], 0)
        self.assertEqual(client.upsert_calls, [])

    def test_successful_split_subbatch_is_persisted_when_later_subbatch_fails(self):
        source_rows = [row(key="key-a", chunk="chunk-a"), row(key="key-b", chunk="chunk-b")]
        client = FakeQdrant()
        calls = []

        def embed(texts, *, is_query=False):
            calls.append(len(texts))
            if len(texts) == 2:
                raise TimeoutError("split this request")
            if len(calls) == 2:
                return [[0.1, 0.2]]
            raise TimeoutError("second reduced request still times out")

        profile = {
            "provider": "openai", "batch_size": 2, "worker_max_retries": 0,
            "retry_backoff_seconds": 0, "adaptive_min_batch_size": 1,
        }
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value=profile), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(result["new_vectors"], 1)
        self.assertEqual(result["vectors_remaining"], 1)
        self.assertEqual([len(batch) for batch in client.upsert_calls], [1])

    def test_permanent_embedding_auth_failure_does_not_retry_or_split(self):
        class Unauthorized(RuntimeError):
            status_code = 401

        source_rows = [row(key="key-a", chunk="chunk-a"), row(key="key-b", chunk="chunk-b")]
        client = FakeQdrant()
        calls = 0

        def embed(texts, *, is_query=False):
            nonlocal calls
            calls += 1
            raise Unauthorized("unauthorized embedding request")

        profile = {
            "provider": "openai", "batch_size": 2, "worker_max_retries": 3,
            "retry_backoff_seconds": 0, "adaptive_min_batch_size": 1,
        }
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value=profile), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(calls, 1)
        self.assertEqual(result["new_vectors"], 0)
        self.assertEqual(client.upsert_calls, [])

    def test_new_point_upserts_are_batched(self):
        source_rows = [row(key=f"key-{index}", chunk=f"chunk-{index}") for index in range(129)]
        client = FakeQdrant()
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value={"provider": "hash", "batch_size": 128}), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection"), \
             mock.patch("rag_backend.embed_texts", side_effect=lambda texts, is_query=False: [[0.1, 0.2] for _ in texts]):
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=source_rows
            )
        self.assertEqual(result["new_vectors"], 129)
        self.assertEqual([len(batch) for batch in client.upsert_calls], [128, 1])

    def test_vector_refresh_preflights_qdrant_before_embedding_and_reports_progress(self):
        source_rows = [row(key=f"key-{index}", chunk=f"chunk-{index}") for index in range(3)]
        client = FakeQdrant()
        client.collection_exists = mock.Mock(return_value=False)
        order = []
        progress = []

        def ensure_collection(*args, **kwargs):
            order.append("qdrant_preflight")

        def embed(texts, *, is_query=False):
            order.append(f"embed:{len(texts)}")
            return [[0.1, 0.2] for _ in texts]

        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embedding_profile", return_value={"provider": "hash", "batch_size": 2}), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="")), \
             mock.patch("rag_backend.expected_embedding_dimension", return_value=2), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"), \
             mock.patch("rag_backend.ensure_qdrant_collection", side_effect=ensure_collection), \
             mock.patch("rag_backend.embed_texts", side_effect=embed):
            result = vector_store.sync_branch_memberships(
                project_id="alpha",
                branch_key="branch:main",
                old_rows=[],
                new_rows=source_rows,
                progress_callback=progress.append,
            )

        self.assertEqual(result["status"], "indexed", result)
        self.assertEqual(order, ["qdrant_preflight", "embed:2", "embed:1"])
        phases = [event.get("phase") for event in progress]
        self.assertIn("qdrant_preflight", phases)
        embedding_events = [event for event in progress if event.get("phase") == "embedding"]
        self.assertEqual([event.get("vectors_persisted") for event in embedding_events], [0, 2, 3])
        self.assertEqual(embedding_events[-1]["chunks_ready"], 3)
        self.assertEqual(embedding_events[-1]["chunks_total"], 3)
        self.assertEqual(embedding_events[-1]["batches_completed"], 2)
        self.assertEqual(embedding_events[-1]["batches_total"], 2)
        self.assertEqual(embedding_events[-1]["progress_percent"], 100.0)

    def test_retrieve_failure_degrades_without_overwriting_or_reembedding(self):
        client = FakeQdrant()
        client.retrieve = mock.Mock(side_effect=RuntimeError("transport failure"))
        with mock.patch("rag_backend.qdrant_client", return_value=(client, "ok")), \
             mock.patch("rag_backend.embed_texts") as embed:
            result = vector_store.sync_branch_memberships(
                project_id="alpha", branch_key="branch:main", old_rows=[], new_rows=[row()]
            )
        self.assertEqual(result["status"], "degraded")
        self.assertIn("transport failure", result["reason"] )
        embed.assert_not_called()
        self.assertEqual(client.upserts, [])
        self.assertEqual(client.payload_updates, [])

    def test_vector_query_failure_is_reported_instead_of_silently_empty(self):
        with mock.patch("rag_backend.qdrant_client", return_value=(None, "qdrant unavailable")), \
             mock.patch("rag_backend.qdrant_collection_name", return_value="fixture"):
            result = vector_store.search_with_status(
                "issuer validation", project_id="alpha", branch_key="branch:main"
            )
        self.assertEqual(result["status"], "degraded")
        self.assertIn("qdrant unavailable", result["reason"])
        self.assertEqual(result["hits"], [])

    def test_embedding_identity_ignores_operational_settings_but_tracks_prefix(self):
        profile_a = {
            "provider": "openai", "model": "tei", "deployment_id": "jina",
            "base_url": "http://tei/v1", "normalize": True,
            "explicit_vector_size": 768, "batch_size": 8, "auth_configured": False,
        }
        profile_b = {**profile_a, "batch_size": 64, "auth_configured": True}
        config_a = SimpleNamespace(document_prefix="search_document: ")
        config_b = SimpleNamespace(document_prefix="search_document: ")
        with mock.patch("rag_backend.embedding_profile", return_value=profile_a), \
             mock.patch("rag_backend.embedding_config", return_value=config_a):
            first = vector_store.embedding_profile_hash()
        with mock.patch("rag_backend.embedding_profile", return_value=profile_b), \
             mock.patch("rag_backend.embedding_config", return_value=config_b):
            second = vector_store.embedding_profile_hash()
        self.assertEqual(first, second)
        with mock.patch("rag_backend.embedding_profile", return_value=profile_b), \
             mock.patch("rag_backend.embedding_config", return_value=SimpleNamespace(document_prefix="different: ")):
            third = vector_store.embedding_profile_hash()
        self.assertNotEqual(first, third)

    def test_membership_hash_is_order_independent(self):
        rows = [row(path="b.py", chunk="b"), row(path="a.py", chunk="a")]
        one = vector_store.membership_hash(rows, "alpha", "branch:main")
        two = vector_store.membership_hash(list(reversed(rows)), "alpha", "branch:main")
        self.assertEqual(one, two)

    def test_payload_contains_paired_project_branch_scope_keys(self):
        first = vector_store._membership_from_row("alpha", "branch:main", row())
        second = vector_store._membership_from_row(
            "beta", "branch:release", row(path="src/shared.py", chunk="chunk-b", repo="beta:repo")
        )
        payload = vector_store._normalize_payload("key", "hash", [first, second])
        self.assertEqual(
            payload["scope_keys"],
            ["alpha\x1fbranch:main", "beta\x1fbranch:release"],
        )
        self.assertNotIn("alpha\x1fbranch:release", payload["scope_keys"])


if __name__ == "__main__":
    unittest.main()
