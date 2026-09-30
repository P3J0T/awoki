"""Unreadable canonical memory is unknown, including when an old index exists."""
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import continuity
import harness_core as core
import project_workspace


class ProjectSearchIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.paths = core.HarnessPaths(self.root, self.root / "global")
        core.project_create("demo", paths=self.paths)
        self.pp = project_workspace.paths_for(self.root, "demo")
        self.note = core.project_capture("Saved orchard investigation", name="demo", kind="direction", paths=self.paths)
        seeded = core.project_search("orchard", name="demo", paths=self.paths)
        self.assertTrue(seeded["project_hits"])

    def search(self, exact=False):
        return core.project_search("orchard", name="demo", include_global=True, paths=self.paths,
                                  **({"record_ids": [self.note["id"]]} if exact else {}))

    def assert_unknown_without_retrieval(self):
        before = self.pp.continuity.read_bytes() if self.pp.continuity.exists() else None
        cached = {p: p.read_bytes() for p in self.pp.index_dir.rglob("*") if p.is_file()}
        with ExitStack() as stack:
            blocked = [stack.enter_context(mock.patch.object(owner, name)) for owner, name in (
                (core, "_ensure_project_exact_index_current"), (core, "index_global"),
                (core.rag_backend, "search_fts"), (core.rag_backend, "search_qdrant"),
                (core.rag_backend, "rerank_hits"), (core.source_references, "annotate_memory"),
            )]
            for exact in (False, True):
                with self.subTest(exact=exact):
                    result = self.search(exact)
                    self.assertEqual(result["status"], "unknown")
                    self.assertEqual(result["reason"], "canonical_state_unavailable")
                    self.assertEqual(result["memory_state"], "unknown")
                    self.assertNotIn("unavailable_ids", result)
                    self.assertNotIn("orchard", json.dumps(result))
            for backend in blocked:
                backend.assert_not_called()
        self.assertEqual(self.pp.continuity.read_bytes() if self.pp.continuity.exists() else None, before)
        self.assertEqual({p: p.read_bytes() for p in self.pp.index_dir.rglob("*") if p.is_file()}, cached)

    def test_corrupt_journal_cannot_return_cached_hits_or_known_absence(self):
        with self.pp.continuity.open("a") as handle:
            handle.write("{incomplete canonical record\n")
        self.assert_unknown_without_retrieval()

    def test_missing_journal_is_unknown_and_not_recreated(self):
        self.pp.continuity.unlink()
        self.assert_unknown_without_retrieval()
        self.assertFalse(self.pp.continuity.exists())

    def test_invalid_scope_and_identity_are_unknown(self):
        for changes in ({"project_id": "other"}, {"id": "bad identity"}, {"tags": "invalid"}):
            with self.subTest(changes=changes):
                self.pp.continuity.write_text(json.dumps({**self.note, **changes}) + "\n")
                self.assert_unknown_without_retrieval()

    def test_read_error_does_not_disclose_error_content(self):
        with mock.patch.object(continuity, "_read_goal_records", side_effect=PermissionError("private location")):
            self.assert_unknown_without_retrieval()
            self.assertNotIn("private location", json.dumps(self.search()))

    def test_valid_empty_journal_and_legacy_records_still_work(self):
        self.pp.continuity.write_text("")
        empty = self.search(exact=True)
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["records"], [])
        self.assertEqual(empty["unavailable_ids"], [self.note["id"]])
        legacy_id = "cont_legacy_orchard"
        (self.pp.memory_dir / "facts.jsonl").write_text(json.dumps({
            "id": legacy_id, "summary": "Legacy orchard observation", "confidence": "medium",
        }) + "\n")
        recalled = core.project_search(name="demo", record_ids=[legacy_id], paths=self.paths)
        self.assertEqual(recalled["status"], "ok")
        self.assertEqual(recalled["records"][0]["id"], legacy_id)
        broad = core.project_search("orchard", name="demo", paths=self.paths)
        self.assertEqual(broad["status"], "ok")
        self.assertIn(legacy_id, {hit.get("record_id") for hit in broad["project_hits"]})

    def test_private_direction_is_not_a_read_failure(self):
        core.project_capture("Restricted saved direction", name="demo", kind="direction",
                             allow_sensitive_plaintext=True, paths=self.paths)
        result = self.search(exact=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["records"][0]["id"], self.note["id"])

    def test_exact_and_broad_recall_reuse_the_validated_snapshot(self):
        original = self.pp.continuity.read_bytes()
        read = continuity._read_goal_records

        def replace_after_read(memory_dir, project_id):
            snapshot = read(memory_dir, project_id)
            self.pp.continuity.write_text("{concurrent incomplete replacement\n")
            return snapshot

        for exact in (False, True):
            with self.subTest(exact=exact):
                self.pp.continuity.write_bytes(original)
                with mock.patch.object(continuity, "_read_goal_records", side_effect=replace_after_read) as strict_read, \
                        mock.patch.object(core, "_ensure_project_exact_index_current", return_value={"status": "current"}), \
                        mock.patch.object(core.rag_backend, "search_fts", return_value=[]), \
                        mock.patch.object(core.rag_backend, "search_qdrant", return_value=[]):
                    result = core.project_search("orchard", name="demo", paths=self.paths,
                                                 **({"record_ids": [self.note["id"]]} if exact else {}))
                self.assertEqual(result["status"], "ok")
                strict_read.assert_called_once()
                refs = ([row["id"] for row in result["records"]] if exact
                        else [row.get("record_id") for row in result["project_hits"]])
                self.assertIn(self.note["id"], refs)
                self.assertEqual(self.pp.continuity.read_text(), "{concurrent incomplete replacement\n")
                # The next call observes the changed journal and reports unknown.
                self.assertEqual(self.search(exact)["status"], "unknown")

    def test_validated_snapshot_preserves_append_order_and_legacy_dedup(self):
        first = continuity.make_record("demo", "First", record_id="cont_z", timestamp="2026-01-01T00:00:00Z")
        second = continuity.make_record("demo", "Second", record_id="cont_a", timestamp="2026-01-01T00:00:00Z")
        (self.pp.memory_dir / "facts.jsonl").write_text(json.dumps({
            "continuity_id": first["id"], "summary": "Duplicated legacy first",
        }) + "\n")
        rows = project_workspace.continuity_records(self.pp, canonical_records=[first, second])
        self.assertEqual([row["id"] for row in rows], [first["id"], second["id"]])


if __name__ == "__main__":
    unittest.main()
