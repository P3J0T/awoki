from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import evidence_store
import harness_core as core
import opencode_events
import project_workspace
import source_references


class SourceReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = core.HarnessPaths(root=self.root, global_root=self.root / "global")
        core.project_create("demo", paths=self.paths)

    def window(self, **updates):
        return {
            "status": "ok", "project_id": "demo", "repo_id": "service", "source_id": "service",
            "revision_key": "rev1", "path": "src/auth.py", "returned": {"start_line": 2, "end_line": 4},
            "evidence_locator": {"source_id": "service", "revision_key": "rev1", "path": "src/auth.py", "start_line": 2, "end_line": 4},
            "evidence": {"evidence_id": "test-token"}, "lines": [{"line": 2, "text": "PRIVATE SOURCE TEXT"}],
            "truncated": True, "redacted": True, **updates,
        }

    def test_metadata_only_content_addressed_and_exact_resolution(self):
        core.project_open("demo", session_id="test-session", paths=self.paths)
        original = self.window()
        first = source_references.capture(self.root, "demo", original, session_id="test-session")
        second = source_references.capture(self.root, "demo", original)
        self.assertNotIn("evidence_ref", original)
        self.assertEqual(first["evidence_ref"], second["evidence_ref"])
        self.assertEqual(first["citation"], "service/src/auth.py:2-4")
        self.assertEqual(source_references.resolve(self.root, "demo", first["evidence_ref"]), ("test-token", None))
        payload = evidence_store.get(self.root, "demo", first["evidence_ref"])["value"]
        self.assertNotIn("PRIVATE SOURCE TEXT", json.dumps(payload))
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["redacted"])
        self.assertIn(first["evidence_ref"], core.session_work_status(session_id="test-session", paths=self.paths).__str__())
        restored = opencode_events.compaction_context(self.root, "test-session", max_chars=4000)["context"]
        self.assertIn(first["evidence_ref"], restored)
        self.assertIn(first["citation"], restored)
        self.assertNotIn("PRIVATE SOURCE TEXT", restored)

    def test_identical_paths_and_text_keep_repository_identity(self):
        one = source_references.capture(self.root, "demo", self.window())
        two = source_references.capture(self.root, "demo", self.window(repo_id="worker", source_id="worker"))
        self.assertNotEqual(one["evidence_ref"], two["evidence_ref"])
        self.assertNotEqual(one["citation"], two["citation"])

    def test_missing_wrong_kind_wrong_project_and_tampered_are_rejected(self):
        ref = source_references.capture(self.root, "demo", self.window())["evidence_ref"]
        self.assertEqual(source_references.resolve(self.root, "other", ref)[1]["verdict"], "INVALID_EVIDENCE")
        self.assertEqual(source_references.resolve(self.root, "demo", "ev_" + "0" * 24)[1]["verdict"], "INVALID_EVIDENCE")
        other = evidence_store.put(self.root, "demo", kind="code_search_result", tool="codebase_search",
                                   payload={"evidence_id": "test-token"}, scope_identity={})
        self.assertEqual(source_references.resolve(self.root, "demo", other["evidence_ref"])[1]["verdict"], "INVALID_EVIDENCE")
        artifact = next(self.root.rglob(ref + ".json.gz"))
        value = json.loads(gzip.decompress(artifact.read_bytes()))
        value["payload"]["evidence_id"] = "forged"
        artifact.write_bytes(gzip.compress(json.dumps(value).encode()))
        self.assertEqual(source_references.resolve(self.root, "demo", ref)[1]["verdict"], "INVALID_EVIDENCE")

    def test_storage_failure_keeps_source_and_legacy_token_with_warning(self):
        with mock.patch.object(evidence_store, "put", side_effect=OSError("private storage path")):
            result = source_references.capture(self.root, "demo", self.window())
        self.assertNotIn("evidence_ref", result)
        self.assertEqual(result["evidence"], self.window()["evidence"])
        self.assertEqual(result["lines"], self.window()["lines"])
        self.assertEqual(result["evidence_reference_warning"], "source_reference_storage_failed")

    def test_failed_window_is_unchanged_and_never_persisted(self):
        result = {"status": "stale_source", "reason": "changed"}
        with mock.patch.object(evidence_store, "put") as put:
            self.assertIs(source_references.capture(self.root, "demo", result), result)
            put.assert_not_called()

    def test_short_ref_always_uses_existing_verifier_and_explicit_scope(self):
        ref = source_references.capture(self.root, "demo", self.window())["evidence_ref"]
        with mock.patch.object(core.code_search, "verify_evidence", return_value={"verdict": "STALE_SOURCE"}) as verify:
            result = core.code_evidence_verify(ref, name="demo", repo="worker", source_id="corpus", paths=self.paths)
            verify.assert_called_once_with(self.paths, "demo", "test-token", repo="worker", source="corpus")
        self.assertEqual(result["verdict"], "STALE_SOURCE")
        self.assertEqual(result["evidence_ref"], ref)
        self.assertIn("not verification of a behavioral claim", result["verification_boundary"])

    def test_real_source_handles_match_legacy_verdict_before_and_after_edit(self):
        import test_search_presentation as fixtures
        with mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}):
            # Separate fixture root: two independent Git repos with identical paths/content.
            paths = fixtures.SearchPresentationTests().fixture(self.root / "integration", repos=("one", "two"))
            windows = [core.code_source_window("auth0.py", name="demo", repo=rid, start_line=1, end_line=3, paths=paths)
                       for rid in ("one", "two")]
            self.assertNotEqual(windows[0]["evidence_ref"], windows[1]["evidence_ref"])
            for rid, window in zip(("one", "two"), windows):
                reopened = core.code_source_window(name="demo", evidence_ref=window["evidence_ref"], paths=paths)
                self.assertEqual(reopened["status"], "ok", reopened)
                self.assertTrue(reopened["reopened"])
                self.assertEqual(reopened["repo_id"], rid)
                self.assertEqual(reopened["lines"], window["lines"])
                self.assertEqual(reopened["returned"], window["returned"])
                short = core.code_evidence_verify(window["evidence_ref"], name="demo", repo=rid, paths=paths)
                full = core.code_evidence_verify(window["evidence"]["evidence_id"], name="demo", repo=rid, paths=paths)
                self.assertEqual(short["verdict"], "CURRENT_VERIFIED_SNAPSHOT")
                self.assertEqual(short["verdict"], full["verdict"])
            source = project_workspace.paths_for(paths.root, "demo").project_dir / "repo/one/auth0.py"
            source.write_text("def changed():\n    return False\n")
            stale = core.code_evidence_verify(windows[0]["evidence_ref"], name="demo", repo="one", paths=paths)
            self.assertNotEqual(stale["verdict"], "CURRENT_VERIFIED_SNAPSHOT")
            stale_read = core.code_source_window(name="demo", evidence_ref=windows[0]["evidence_ref"], paths=paths)
            self.assertFalse(stale_read["reopened"])
            self.assertNotIn("lines", stale_read)
            wrong = core.code_source_window(name="demo", evidence_ref=windows[1]["evidence_ref"], repo="one", paths=paths)
            self.assertEqual(wrong["verdict"], "REPOSITORY_MISMATCH")

    def test_reopen_rejects_mixed_navigation_and_missing_reference_without_reading(self):
        with mock.patch.object(core.code_search, "source_window") as read:
            for arguments in ({"path": "src/auth.py"}, {"start_line": 2}, {"end_line": 3}, {"refresh_index": True}, {}):
                result = core.code_source_window(name="demo", evidence_ref="ev_missing", paths=self.paths, **arguments)
                self.assertEqual(result["status"], "rejected")
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
