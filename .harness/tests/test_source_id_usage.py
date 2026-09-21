from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import continuity
import evidence_store
import harness_core as core
import project_workspace
import source_references
from code_search import engine


class SourceIdUsageTests(unittest.TestCase):
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

    def window(self):
        return {"status": "ok", "project_id": "demo", "repo_id": "", "source_id": "corpus",
                "revision_key": "revision", "path": "check.py", "returned": {"start_line": 1, "end_line": 2},
                "evidence_locator": {"source_id": "corpus", "path": "check.py", "start_line": 1, "end_line": 2},
                "evidence": {"evidence_id": "ev5z.PRIVATE_TOKEN_PAYLOAD.0123456789abcdef"},
                "lines": [{"line": 1, "text": "PRIVATE_SOURCE_TEXT"}], "truncated": False}

    def test_copyable_continuity_reference_preserves_legacy_token_and_storage_identity(self):
        original = self.window()
        before = deepcopy(original)
        result = source_references.capture(self.root, "demo", original)
        self.assertEqual(original, before)
        self.assertEqual(result["evidence"], original["evidence"])
        self.assertEqual(result["continuity_sources"], [result["evidence_ref"]])
        self.assertIn("project_capture", result["reference_usage"])
        self.assertIn("legacy code_evidence_verify token", result["reference_usage"])
        self.assertEqual(source_references.memory_source_issues(self.root, "demo", result["continuity_sources"]), [])
        again = source_references.capture(self.root, "demo", original)
        self.assertEqual(result["evidence_ref"], again["evidence_ref"])
        stored = evidence_store.get(self.root, "demo", result["evidence_ref"])["value"]
        self.assertNotIn("continuity_sources", stored)
        self.assertNotIn("reference_usage", stored)
        self.assertNotIn("PRIVATE_SOURCE_TEXT", json.dumps(stored))

    def test_storage_failure_never_advertises_a_durable_reference(self):
        original = self.window()
        with mock.patch.object(evidence_store, "put", side_effect=OSError("private path")):
            result = source_references.capture(self.root, "demo", original)
        self.assertNotIn("continuity_sources", result)
        self.assertNotIn("reference_usage", result)
        self.assertNotIn("evidence_ref", result)
        self.assertEqual(result["evidence"], original["evidence"])
        self.assertEqual(result["evidence_reference_warning"], "source_reference_storage_failed")

    def test_raw_and_normalized_legacy_shapes_reject_without_token_echo_or_lookup(self):
        with mock.patch.object(evidence_store, "metadata") as metadata, \
                mock.patch.object(evidence_store, "get") as get:
            for version in (3, 4, 5):
                token = f"ev{version}z.PRIVATE_TOKEN_PAYLOAD.0123456789abcdef"
                values = [token, *({field: token} for field in
                    ("id", "ref", "evidence_ref", "evidence_id", "path", "uri"))]
                for value in values:
                    with self.subTest(version=version, shape=type(value).__name__, value=value):
                        issues = source_references.memory_source_issues(self.root, "demo", ["ordinary.py", value])
                        self.assertEqual(len(issues), 1)
                        self.assertEqual(issues[0]["source_index"], 1)
                        self.assertEqual(issues[0]["reason"], "legacy_source_token")
                        self.assertIn("evidence_ref", issues[0]["instruction"])
                        self.assertIn("do not remove evidence", issues[0]["instruction"])
                        self.assertNotIn("PRIVATE_TOKEN_PAYLOAD", json.dumps(issues))
            normalized = continuity.normalize_sources(["ev5z." + "x" * 200 + ".0123456789abcdef"])
            self.assertEqual(source_references.memory_source_issues(self.root, "demo", normalized)[0]["reason"],
                             "legacy_source_token")
            long_token = "ev5z." + "x" * 4000 + ".0123456789abcdef"
            self.assertEqual(source_references.memory_source_issues(self.root, "demo", [long_token])[0]["reason"],
                             "legacy_source_token")
            metadata.assert_not_called()
            get.assert_not_called()

    def test_token_prefix_filenames_are_allowed_without_guessing_malformed_tokens(self):
        for version in (3, 4, 5):
            filename = f"ev{version}z.notes.md"
            sources = [filename, {"path": filename}, {"uri": filename}, f"src/{filename}",
                       f"ev{version}z.incomplete-token"]
            self.assertEqual(source_references.memory_source_issues(self.root, "demo", sources), [])
            saved = core.project_capture(f"Documented token version {version}", name="demo", sources=[filename],
                                         paths=self.paths)
            self.assertEqual(saved["status"], "captured", saved)
            self.assertEqual(saved["sources"][0]["path"], filename)
            for field in ("id", "ref", "evidence_ref", "evidence_id"):
                self.assertEqual(source_references.memory_source_issues(self.root, "demo", [{field: filename}])[0]["reason"],
                                 "legacy_source_token")

    def test_public_capture_rejects_legacy_before_reconciliation_or_write_including_based_on(self):
        parent = core.project_capture("Base observation", name="demo", uncertainty=["Not runtime verified"],
                                      sources=[f"known/{i}.py" for i in range(100)], paths=self.paths)
        self.assertEqual(parent["status"], "captured", parent)
        before = self.pp.continuity.read_bytes()
        token = self.window()["evidence"]["evidence_id"]
        with mock.patch.object(core, "_capture_reconciliation", side_effect=AssertionError("must reject before reconciliation")), \
                mock.patch.object(project_workspace, "project_capture", side_effect=AssertionError("must not write")):
            for based_on in ([], [parent["id"]]):
                for source in (token, {"id": token}, {"ref": token}, {"evidence_ref": token}, {"path": token},
                               "ev5z." + "x" * 4000 + ".0123456789abcdef"):
                    with self.subTest(based_on=bool(based_on), source=source):
                        sources = [f"declared/{i}.py" for i in range(99)] + [source]
                        result = core.project_capture("New scoped observation", name="demo", sources=sources,
                                                      based_on=based_on, paths=self.paths)
                        self.assertEqual(result["status"], "rejected", result)
                        self.assertEqual(result["written"], 0)
                        self.assertEqual(result["invalid_sources"][0]["source_index"], 99)
                        self.assertEqual(result["invalid_sources"][0]["reason"], "legacy_source_token")
                        self.assertNotIn("PRIVATE_TOKEN_PAYLOAD", json.dumps(result))
                        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_plain_citations_and_saved_handles_keep_existing_validation(self):
        generic = evidence_store.put(self.root, "demo", kind="code_search_result", tool="codebase_search",
                                     payload={"query": "scope"}, scope_identity={})["evidence_ref"]
        source = source_references.capture(self.root, "demo", self.window())["evidence_ref"]
        sources = ["src/ev5z.example.py", "ordinary.py", "project://demo/known-note",
                   {"path": "auth.py", "description": "ev5z. is a token prefix"}, generic,
                   {"evidence_ref": source}, {"ref": source}]
        self.assertEqual(source_references.memory_source_issues(self.root, "demo", sources), [])
        self.assertEqual(source_references.memory_source_issues(self.root, "other", [source])[0]["reason"],
                         "missing_corrupt_or_wrong_project")
        self.assertEqual(source_references.memory_source_issues(self.root, "demo", [{"id": source, "path": "wrong.py"}])[0]["reason"],
                         "source_path_mismatch")
        with mock.patch.object(source_references, "memory_source_issues", wraps=source_references.memory_source_issues) as validate:
            saved = core.project_capture("Useful search artifact", name="demo", sources=[generic], paths=self.paths)
        self.assertEqual(saved["status"], "captured", saved)
        self.assertEqual(saved["sources"][0]["id"], generic)
        validate.assert_called_once_with(self.root, "demo", [generic])

    def test_actual_ev5_token_stays_verifiable_while_durable_note_uses_short_ref(self):
        source = self.pp.project_dir / "sources/corpus"
        source.mkdir(parents=True)
        file = source / "check.py"
        file.write_text("def check(value):\n    return value is not None\n")
        project_workspace.project_source_add(self.root, "demo", "corpus", "sources/corpus")
        indexed = engine.index_project_code(self.paths, "demo", source="corpus", include_qdrant=False)
        self.assertEqual(indexed["status"], "indexed", indexed)
        window = core.code_source_window("check.py", name="demo", source_id="corpus", start_line=1,
                                         end_line=2, paths=self.paths)
        self.assertEqual(window["status"], "ok", window)
        token = window["evidence"]["evidence_id"]
        self.assertTrue(token.startswith("ev5z."))
        before = self.pp.continuity.read_bytes()
        rejected = core.project_capture("Scoped source condition", name="demo", sources=[token], paths=self.paths)
        self.assertEqual(rejected["status"], "rejected", rejected)
        self.assertEqual(self.pp.continuity.read_bytes(), before)
        legacy = core.code_evidence_verify(token, name="demo", source_id="corpus", paths=self.paths)
        short = core.code_evidence_verify(window["evidence_ref"], name="demo", source_id="corpus", paths=self.paths)
        self.assertTrue(legacy["current"], legacy)
        self.assertEqual(legacy["verdict"], short["verdict"])
        saved = core.project_capture("Scoped source condition", name="demo", sources=window["continuity_sources"],
                                     uncertainty=["Static source only"], paths=self.paths)
        self.assertEqual(saved["status"], "captured", saved)
        self.assertEqual(saved["sources"][0]["id"], window["evidence_ref"])
        self.assertEqual(saved["uncertainty"], ["Static source only"])
        file.write_text("def check(value):\n    return False\n")
        stale_legacy = core.code_evidence_verify(token, name="demo", source_id="corpus", paths=self.paths)
        stale_short = core.code_evidence_verify(window["evidence_ref"], name="demo", source_id="corpus", paths=self.paths)
        self.assertFalse(stale_legacy.get("current"))
        self.assertEqual(stale_legacy["verdict"], stale_short["verdict"])


if __name__ == "__main__":
    unittest.main()
