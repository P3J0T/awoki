from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import harness_core as core
import continuity
import memory_recall
import project_workspace
import rag_backend


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = core.HarnessPaths(self.root, self.root / "global")
        env = mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1", "AWOKI_RERANK_ENABLED": "0"})
        env.start()
        self.addCleanup(env.stop)
        core.project_create("demo", paths=self.paths)
        self.pp = project_workspace.paths_for(self.root, "demo")

    def save(self, summary="Source observation", **kwargs):
        return core.project_capture(summary, name="demo", paths=self.paths, **kwargs)

    def get(self, ids, **kwargs):
        return core.project_search(name="demo", record_ids=ids, paths=self.paths, **kwargs)

    def test_batch_keeps_independent_scope_and_caveats_and_is_idempotent(self):
        items = [{"summary": "Checks a credential", "sources": [{"repo": repo, "path": "auth.py"}],
                  "uncertainty": [repo + " runtime not tested"]} for repo in ("service", "worker")]
        saved = self.save("", items=items)
        self.assertEqual(saved["status"], "captured")
        self.assertEqual(saved["written"], 2)
        self.assertFalse(saved["atomic"])
        ids = [row["id"] for row in saved["items"]]
        self.assertEqual(len(set(ids)), 2)
        for repo, row in zip(("service", "worker"), saved["items"]):
            self.assertEqual(row["sources"][0]["repo"], repo)
            self.assertEqual(row["uncertainty"], [repo + " runtime not tested"])
        again = self.save("", items=items)
        self.assertEqual(again["written"], 0)
        self.assertEqual([row["id"] for row in again["items"]], ids)

    def test_batch_prevalidates_every_item_without_writes(self):
        before = self.pp.continuity.read_bytes()
        for bad in ([], [{}], [{"summary": "a"}] * 13,
                    [{"summary": "good"}, {"summary": "bad", "details": "x" * 2001}],
                    [{"summary": "bad", "name": "other"}], [{"summary": "bad", "uncertainty": "text"}]):
            with self.subTest(items=bad):
                self.assertEqual(self.save("", items=bad)["status"], "rejected")
                self.assertEqual(self.pp.continuity.read_bytes(), before)
        self.assertEqual(self.save("mixed input", items=[{"summary": "item"}])["status"], "rejected")

    def test_batch_partial_result_does_not_claim_all_saved(self):
        with mock.patch.object(core, "_capture_reconciliation", side_effect=[
            {"classification": "possible_contradiction", "matches": []},
            {"classification": "new", "matches": []},
        ]):
            result = self.save("", items=[{"summary": "Needs review"}, {"summary": "Independent observation"}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["written"], 1)
        self.assertEqual([row["status"] for row in result["items"]], ["needs_review", "captured"])

    def test_plain_exploration_note_still_needs_no_sources(self):
        saved = self.save("Prefer shorter answers")
        self.assertEqual(saved["source_binding"]["status"], "unbound")
        recalled = self.get([saved["id"]])["records"][0]
        self.assertEqual(recalled["summary"], "Prefer shorter answers")
        self.assertTrue(recalled["complete"])
        self.assertFalse(recalled["source_freshness"]["claim_verified"])

    def test_evidence_refs_alias_preserves_handles_alongside_plain_sources(self):
        import source_references
        from test_source_references import SourceReferenceTests
        ref = source_references.capture(self.root, "demo", SourceReferenceTests().window())["evidence_ref"]
        items = [{"summary": "Bound code observation", "sources": ["src/auth.py"],
                  "evidence_refs": [ref], "uncertainty": ["External callback behavior unknown"]}]
        saved = self.save("", items=items)["items"][0]
        self.assertIn(ref, [s.get("id") for s in saved["sources"]])
        self.assertEqual(saved["source_binding"]["status"], "mixed")
        self.assertFalse(saved["source_binding"]["claim_verified"])
        again = self.save("", items=items)["items"][0]
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["sources"], saved["sources"])
        self.assertEqual(again["uncertainty"], items[0]["uncertainty"])

    def test_invalid_evidence_refs_never_downgrade_to_plain_paths(self):
        before = self.pp.continuity.read_bytes()
        for refs in (["ev_missing"], ["src/auth.py"], "ev_missing"):
            result = self.save("Observation", sources=["src/auth.py"], evidence_refs=refs)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(self.pp.continuity.read_bytes(), before)
        result = self.save("Many references", sources=["auth.py"] * 100, evidence_refs=["ev_missing"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_exact_recall_recovers_details_without_backend_or_writes(self):
        details = "Earlier details. " * 150 + "The late finding must survive."
        saved = self.save(details=details, uncertainty=["Runtime not executed"])
        before = self.pp.continuity.read_bytes()
        with mock.patch.object(core, "_ensure_project_exact_index_current") as refresh, \
             mock.patch.object(rag_backend, "search_fts") as fts, \
             mock.patch.object(rag_backend, "search_qdrant") as vector, \
             mock.patch.object(rag_backend, "rerank_hits") as rerank:
            result = self.get([saved["id"]])["records"][0]
        for check in (refresh, fts, vector, rerank):
            check.assert_not_called()
        self.assertEqual(result["details"], details)
        self.assertEqual(result["uncertainty"], ["Runtime not executed"])
        self.assertTrue(result["complete"])
        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_exact_pages_details_caveats_and_sources_without_silent_loss(self):
        details = "0123456789" * 1400
        uncertainty = [f"Caveat {n}" for n in range(19)]
        sources = [{"repo": "one", "path": f"file{n}.py"} for n in range(19)]
        saved = self.save(details=details, uncertainty=uncertainty, sources=sources)
        first = self.get([saved["id"]], max_chars=1024)["records"][0]
        self.assertFalse(first["complete"])
        recovered = {key: first[key] for key in ("details", "uncertainty", "sources")}
        queue = first["next_calls"][:]
        while queue:
            call = queue.pop(0)
            section = call["section"]
            page = core.project_search(paths=self.paths, **call)["records"][0]
            recovered[section] += page[section]
            queue.extend(page["next_calls"])
        self.assertEqual(recovered, {"details": details, "uncertainty": uncertainty, "sources": saved["sources"]})

    def test_private_superseded_missing_and_wrong_project_records_unavailable(self):
        private = self.save("Private note", allow_sensitive_plaintext=True)
        old = self.save("Old note")
        new = self.save("New correction", kind="correction", supersedes=[old["id"]])
        result = self.get([private["id"], old["id"], "cont_missing"])
        self.assertEqual(result["records"], [])
        self.assertNotIn("Private note", json.dumps(result))
        core.project_create("other", paths=self.paths)
        other = core.project_search(name="other", record_ids=[new["id"]], paths=self.paths)
        self.assertEqual(other["records"], [])
        self.assertEqual(self.get([new["id"]])["records"][0]["summary"], "New correction")

    def test_search_exposes_truncation_and_one_canonical_hit(self):
        saved = self.save("Authorization across repositories", details="A detail. " * 300 + "Late middleware finding")
        result = core.project_search("Authorization", name="demo", paths=self.paths)
        hits = [hit for hit in result["project_hits"] if hit.get("record_id") == saved["id"]]
        self.assertEqual(len(hits), 1)
        note = hits[0]["metadata"]
        self.assertFalse(note["complete"])
        self.assertTrue(note["next_calls"])
        self.assertNotIn("memory_reconciliation", json.dumps(note))
        self.assertIn("jsonl_scan", hits[0]["retrieval_backends"])

    def test_stale_derived_private_hits_are_removed_and_current_content_hydrated(self):
        saved = self.save("Actual observation")
        stale = {"metadata": {"id": saved["id"]}, "preview": "obsolete", "scope": None}
        missing = {"metadata": {"id": "cont_private"}, "preview": "private content"}
        result = memory_recall.canonical_hits([stale, missing], {saved["id"]: saved}, "demo")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["scope"], "project")
        self.assertIn("Actual observation", result[0]["preview"])
        self.assertNotIn("private content", json.dumps(result))

    def test_rag_dedup_uses_project_identity_not_path_aliases(self):
        a = {"scope": "project", "project_id": "a", "kind": "observation", "source_path": "relative", "line": 1, "metadata": {"id": "cont_1"}, "score": 1, "retrieval_backend": "sqlite_fts"}
        b = {**a, "source_path": "/absolute/relative", "retrieval_backend": "jsonl_scan"}
        other = {**a, "project_id": "b"}
        self.assertEqual(len(rag_backend.merge_hits([a], [b], limit=10)), 1)
        self.assertEqual(len(rag_backend.merge_hits([a, other], limit=10)), 2)

    def test_invalid_exact_read_shape_is_rejected(self):
        for ids, section in (([], "record"), (["other"], "record"), (["cont_x"] * 4, "record"), (["cont_x"], "invalid"), (["cont_a", "cont_b"], "details")):
            self.assertEqual(self.get(ids, section=section)["status"], "rejected")

    def test_real_bound_source_ranges_survive_exact_recall(self):
        from test_search_presentation import SearchPresentationTests
        paths = SearchPresentationTests().fixture(self.root / "fixture", repos=("one", "two"))
        window = core.code_source_window("auth0.py", name="demo", repo="one", start_line=1, end_line=3, paths=paths)
        saved = core.project_capture(name="demo", items=[{"summary": "Scoped source observation", "sources": [window["evidence_ref"]]}], paths=paths)["items"][0]
        result = core.project_search(name="demo", record_ids=[saved["id"]], paths=paths)["records"][0]
        self.assertEqual(result["sources"][0]["line_start"], 1)
        self.assertEqual(result["sources"][0]["line_end"], 3)
        self.assertEqual(result["source_freshness"]["status"], "current")
        self.assertFalse(result["source_freshness"]["claim_verified"])

    def test_source_object_evidence_ref_alias_from_live_qwen_is_preserved(self):
        from test_search_presentation import SearchPresentationTests
        paths = SearchPresentationTests().fixture(self.root / "alias-fixture", repos=("one", "two"))
        window = core.code_source_window("auth0.py", name="demo", repo="one", start_line=1, end_line=3, paths=paths)
        ref = window["evidence_ref"]
        saved = core.project_capture(name="demo", items=[{
            "summary": "Observed source range", "sources": [{"source_id": "one", "path": "auth0.py", "evidence_ref": ref, "lines": [1, 3]}],
        }], paths=paths)["items"][0]
        self.assertEqual(saved["sources"][0]["id"], ref)
        self.assertEqual(saved["sources"][0]["line_start"], 1)
        self.assertEqual(saved["sources"][0]["line_end"], 3)
        self.assertEqual(saved["capture_advice"], [])
        exact = core.project_search(name="demo", record_ids=[saved["id"]], paths=paths)["records"][0]
        self.assertEqual(exact["source_freshness"]["status"], "current")

    def test_conflicting_evidence_aliases_fail_closed(self):
        for other in ("id", "ref"):
            normalized = continuity.normalize_sources([{"evidence_ref": "ev_one", other: "ev_two"}])
            self.assertEqual(normalized[0]["id"], "ev_invalid_conflicting_source_handles")
            self.assertNotIn("evidence_ref", normalized[0])


if __name__ == "__main__":
    unittest.main()
