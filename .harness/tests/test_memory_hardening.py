from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import continuity
import harness_core as core
import project_workspace
import source_references


class MemoryHardeningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = core.HarnessPaths(root=self.root, global_root=self.root / "global")
        self.env = mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1", "AWOKI_RERANK_ENABLED": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        core.project_create("demo", paths=self.paths)
        self.pp = project_workspace.paths_for(self.root, "demo")

    def capture(self, **kwargs):
        return core.project_capture("Worker checks authorization.", name="demo", paths=self.paths, **kwargs)

    def test_caveat_and_lower_confidence_create_append_only_revision(self):
        first = self.capture(sources=[{"repo": "worker", "path": "auth.py"}], confidence="high")
        before = self.pp.continuity.read_bytes()
        second = self.capture(confidence="low", uncertainty=["External plugins were not inspected."])
        self.assertEqual(second["status"], "captured")
        self.assertEqual(second["supersedes"], [first["id"]])
        self.assertEqual(second["sources"], first["sources"])
        self.assertEqual(second["confidence"], "low")
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))
        active = project_workspace.continuity_records(self.pp)
        self.assertNotIn(first["id"], [r["id"] for r in active])
        again = self.capture()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], second["id"])
        self.assertIn("External plugins", self.pp.handoff.read_text())

    def test_confidence_only_update_is_not_duplicate(self):
        first = self.capture()
        second = self.capture(confidence="low")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["confidence"], "low")

    def test_state_tags_and_privacy_are_not_duplicate(self):
        self.capture()
        second = self.capture(state="blocked", tags=["follow-up"])
        self.assertEqual(second["status"], "captured")
        private = self.capture(index_policy="no_rag")
        self.assertEqual(private["status"], "captured")
        self.assertEqual(private["state"], "blocked")
        self.assertEqual(private["tags"], ["follow-up"])
        hits = core.project_search("Worker checks authorization", name="demo", paths=self.paths)["project_hits"]
        self.assertFalse(any(h.get("metadata", {}).get("id") == private["id"] for h in hits))
        resumed = core.project_resume("demo", paths=self.paths)
        # Session bookkeeping may retain an opaque last-capture ID, never content.
        resumed.pop("session", None)
        self.assertNotIn(private["id"], json.dumps(resumed))

    def test_new_kind_and_different_repository_are_not_merged(self):
        first = self.capture(kind="observation", sources=[{"repo": "worker", "path": "auth.py"}])
        other = self.capture(kind="observation", sources=[{"repo": "service", "path": "auth.py"}])
        finding = self.capture(kind="finding")
        self.assertEqual(other["supersedes"], [])
        self.assertEqual(finding["supersedes"], [])
        self.assertEqual(len({first["id"], other["id"], finding["id"]}), 3)

    def test_exact_or_private_capture_never_queries_semantic_duplicate_backend(self):
        self.capture()
        with mock.patch.object(core, "_project_vector_is_current", return_value=True), \
             mock.patch.object(core.rag_backend, "search_qdrant") as remote:
            self.capture()
            self.capture(details="Unique private extension", index_policy="no_rag")
            self.capture(details="Uppercase policy extension", index_policy="NO_RAG")
            self.capture(details="Private alias extension", sensitivity="private")
            self.capture(details="Unknown policy extension", index_policy="unknown")
            remote.assert_not_called()

    def test_lower_level_fingerprint_supports_old_history_and_confidence(self):
        record = continuity.make_record("demo", "Local note")
        old = {**record, "fingerprint": "pre-upgrade-fingerprint"}
        path = self.root / "history.jsonl"
        path.write_text(json.dumps(old) + "\n")
        self.assertEqual(continuity.append_record(path, record)["_write_status"], "duplicate_skipped")
        lower = continuity.make_record("demo", "Local note", confidence="low")
        self.assertEqual(continuity.append_record(path, lower)["_write_status"], "appended")

    def test_short_source_handles_accept_string_id_and_ref(self):
        reference = "ev_" + "a" * 24
        expected = [{"type": "reference", "id": reference}]
        self.assertEqual(continuity.normalize_sources([reference]), expected)
        self.assertEqual(continuity.normalize_sources([{"ref": reference}]), expected)
        self.assertEqual(continuity.normalize_sources([{"id": reference}]), expected)

    def test_freshness_budget_is_per_call_and_never_claim_verification(self):
        records = [{"sources": [{"id": "ev_" + str(n)}]} for n in range(4)]
        with mock.patch.object(source_references, "resolve", return_value=("token", None)) as resolve, \
             mock.patch("code_search.engine.verify_evidence", return_value={"current": True, "verdict": "CURRENT_VERIFIED_SNAPSHOT"}):
            result = source_references.annotate_memory(self.root, "demo", records, max_checks=2)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual([r["source_freshness"]["status"] for r in result], ["current", "current", "unverified", "unverified"])
            self.assertFalse(any(r["source_freshness"]["claim_verified"] for r in result))
        self.assertNotIn("source_freshness", records[0])

    def test_real_source_search_resume_stale_missing_and_wrong_project(self):
        from test_search_presentation import SearchPresentationTests
        # The helper registers independent, identical repositories and indexes locally.
        paths = SearchPresentationTests().fixture(self.root / "fixture", repos=("one", "two"))
        window = core.code_source_window("auth0.py", name="demo", repo="one", start_line=1, end_line=3, paths=paths)
        ref = window["evidence_ref"]
        saved = core.project_capture("Authentication token checked.", name="demo", sources=[ref], paths=paths)
        self.assertEqual(saved["sources"][0]["repo"], "one")
        self.assertEqual(saved["sources"][0]["path"], "auth0.py")
        def recall():
            result = core.project_search("Authentication token checked", name="demo", paths=paths)
            return next(h for h in result["project_hits"] if h.get("metadata", {}).get("id") == saved["id"])
        self.assertEqual(recall()["source_freshness"]["status"], "current")
        target = project_workspace.paths_for(paths.root, "demo").project_dir / "repo/one/auth0.py"
        target.write_text(target.read_text().replace("is not None", "== 'admin'"))
        self.assertEqual(recall()["source_freshness"]["status"], "stale")
        resumed = core.project_resume("demo", paths=paths)
        entries = resumed["important_knowledge"] + resumed["changes_since_previous_handoff"]
        self.assertTrue(any(r.get("source_freshness", {}).get("status") == "stale" for r in entries))
        core.project_create("other", paths=paths)
        wrong = source_references.annotate_memory(paths.root, "other", [saved])[0]
        self.assertEqual(wrong["source_freshness"]["status"], "unverified")
        missing = source_references.annotate_memory(paths.root, "demo", [{"sources": ["ev_missing"]}])[0]
        self.assertEqual(missing["source_freshness"]["status"], "unverified")
        unbound = source_references.annotate_memory(paths.root, "demo", [{"summary": "User prefers short answers"}])[0]
        self.assertEqual(unbound["source_freshness"]["status"], "unbound")
