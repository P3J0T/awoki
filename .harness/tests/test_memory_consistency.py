from __future__ import annotations

import json
import unittest
from unittest import mock

import continuity
import harness_core as core
import memory_recall
import project_workspace
import source_references
import test_memory_recall


class MemoryConsistencyTests(unittest.TestCase):
    setUp = test_memory_recall.MemoryRecallTests.setUp
    save = test_memory_recall.MemoryRecallTests.save
    get = test_memory_recall.MemoryRecallTests.get

    def parent(self, **kwargs):
        return self.save("Router invokes an external callback", sources=[{"repo": "web", "path": "router.ts"}],
                         uncertainty=["Authorization inside that callback is unknown."], **kwargs)

    def test_details_only_read_keeps_qualifications_and_followup_arguments(self):
        first = self.parent(details="The enclosing branch calls the supplied function.")
        result = self.get([first["id"]], section="details")["records"][0]
        self.assertEqual(result["qualifications"]["uncertainty"], first["uncertainty"])
        self.assertFalse(result["qualifications"]["source_binding"]["claim_verified"])
        self.assertEqual(result["followup_capture"]["arguments"]["based_on"], [first["id"]])
        self.assertEqual(result["next_calls"], [])

    def test_qualification_preview_is_bounded_without_recursive_section_calls(self):
        saved = self.save("Large qualified note", details="detail", uncertainty=[f"Caveat {i}" for i in range(15)])
        details = self.get([saved["id"]], section="details")["records"][0]
        q = details["qualifications"]
        self.assertEqual(len(q["uncertainty"]), 3)
        self.assertFalse(q["complete"])
        self.assertEqual(q["uncertainty_count"], 15)
        self.assertFalse(details["next_calls"])
        page = core.project_search(paths=self.paths, **q["read_all"])["records"][0]
        self.assertEqual(page["uncertainty"], saved["uncertainty"][:8])
        self.assertEqual([c["offset"] for c in page["next_calls"]], [8])

    def test_missing_caveats_are_not_claimed_to_be_absent(self):
        saved = self.save("User prefers concise answers")
        row = self.get([saved["id"]])["records"][0]
        self.assertIn("not_recorded", row["qualifications"]["uncertainty_state"])
        self.assertEqual(row["qualifications"]["source_binding"]["status"], "unbound")

    def test_paraphrase_inherits_caveats_sources_and_confidence_append_only(self):
        parent = self.parent(confidence="low")
        before = self.pp.continuity.read_bytes()
        with mock.patch.object(core, "_project_vector_is_current", return_value=True), \
             mock.patch.object(core.rag_backend, "search_qdrant") as remote:
            saved = self.save("Dispatch delegates the operation to a plugin", based_on=[parent["id"]], confidence="high")
        remote.assert_not_called()
        self.assertEqual(saved["status"], "captured")
        self.assertEqual(saved["confidence"], "low")
        self.assertEqual(saved["sources"], parent["sources"])
        self.assertEqual(saved["uncertainty"], parent["uncertainty"])
        self.assertEqual(saved["supersedes"], [])
        self.assertFalse(saved["preservation"]["claim_verified"])
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))
        active = project_workspace.continuity_records(self.pp)
        self.assertIn(parent["id"], [r["id"] for r in active])
        self.assertIn(parent["uncertainty"][0], self.pp.handoff.read_text())
        # Restart-style canonical read does not depend on process state.
        self.assertEqual(self.get([saved["id"]], section="details")["records"][0]["qualifications"]["uncertainty"], parent["uncertainty"])

    def test_only_explicit_parents_inherit_and_unrelated_notes_stay_flexible(self):
        parent = self.parent()
        unrelated = self.save("A worker-only reminder", sources=[{"repo": "worker", "path": "router.ts"}])
        saved = self.save("Plugin follow-up", based_on=[parent["id"]])
        self.assertEqual(saved["sources"], parent["sources"])
        self.assertNotIn(unrelated["sources"][0], saved["sources"])
        casual = self.save("Use blue headings")
        self.assertEqual(casual["status"], "captured")
        self.assertEqual(casual["uncertainty"], [])

    def test_missing_private_retired_and_cross_project_parents_fail_closed(self):
        private = self.save("Secret parent", sensitivity="secret")
        old = self.parent()
        self.save("Resolved old note", kind="correction", supersedes=[old["id"]])
        core.project_create("other", paths=self.paths)
        other = core.project_capture("Other project", name="other", paths=self.paths)
        before = self.pp.continuity.read_bytes()
        for parent_id in ("cont_missing", private["id"], old["id"], other["id"]):
            with self.subTest(parent=parent_id):
                result = self.save("Derived note", based_on=[parent_id])
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["unavailable_ids"], [parent_id])
                self.assertNotIn("Secret parent", json.dumps(result))
                self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_invalid_inherited_evidence_is_not_silently_removed(self):
        legacy = project_workspace.project_capture(self.root, "demo", "Legacy note", sources=["ev_missing"])
        before = self.pp.continuity.read_bytes()
        result = self.save("A linked note", based_on=[legacy["id"]])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["invalid_sources"][0]["evidence_ref"], "ev_missing")
        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_explicit_correction_can_resolve_a_caveat_without_rewriting_history(self):
        parent = self.parent()
        before = self.pp.continuity.read_bytes()
        mixed = self.save("Correction", kind="correction", supersedes=[parent["id"]], based_on=[parent["id"]])
        self.assertEqual(mixed["status"], "rejected")
        saved = self.save("Callback implementation inspected separately", kind="correction", supersedes=[parent["id"]], sources=parent["sources"])
        self.assertEqual(saved["uncertainty"], [])
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))

    def test_batch_inheritance_and_prevalidation(self):
        parent = self.parent()
        before = self.pp.continuity.read_bytes()
        for value in (["cont_a"] * 4, "cont_a", ["ev_wrong"]):
            result = self.save("", items=[{"summary": "valid"}, {"summary": "bad", "based_on": value}])
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(self.pp.continuity.read_bytes(), before)
        result = self.save("", items=[{"summary": "Linked observation", "based_on": [parent["id"]]},
                                      {"summary": "Independent preference"}])
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["items"][0]["uncertainty"], parent["uncertainty"])
        self.assertEqual(result["items"][1]["uncertainty"], [])

    def test_inheritance_limits_do_not_drop_qualifications(self):
        parents = [project_workspace.project_capture(self.root, "demo", f"Bulk {i}",
                   sources=[{"repo": str(i), "path": f"{j}.py"} for j in range(60)]) for i in range(2)]
        before = self.pp.continuity.read_bytes()
        result = self.save("Aggregate", based_on=[p["id"] for p in parents])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.pp.continuity.read_bytes(), before)

    def test_lineage_participates_in_dedupe_and_exact_restatement(self):
        a = self.save("Parent alpha")
        b = self.save("Parent beta")
        first = self.save("Same derived text", based_on=[a["id"]])
        second = self.save("Same derived text", based_on=[b["id"]])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["supersedes"], [])
        repeat = self.save("Same derived text", based_on=[b["id"]])
        self.assertEqual(repeat["status"], "duplicate")
        self.assertEqual(repeat["id"], second["id"])
        again = self.save("Same derived text")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], second["id"])

    def test_arbitrary_metadata_cannot_spoof_validated_lineage(self):
        result = self.save("Ordinary note", metadata={"based_on": ["cont_private"]})
        self.assertNotIn("based_on", result.get("metadata", {}))

    def test_bound_evidence_and_staleness_survive_followup(self):
        from test_search_presentation import SearchPresentationTests
        paths = SearchPresentationTests().fixture(self.root / "bound", repos=("one", "two"))
        window = core.code_source_window("auth0.py", name="demo", repo="one", start_line=1, end_line=3, paths=paths)
        parent = core.project_capture("The source checks a value", name="demo", sources=[window["evidence_ref"]],
                                      uncertainty=["Execution was not observed"], paths=paths)
        target = project_workspace.paths_for(paths.root, "demo").project_dir / "repo/one/auth0.py"
        target.write_text(target.read_text().replace("is not None", "== 'admin'"))
        followup = core.project_capture("Historical source check", name="demo", based_on=[parent["id"]], paths=paths)
        recalled = core.project_search(name="demo", record_ids=[followup["id"]], paths=paths)["records"][0]
        self.assertEqual(recalled["source_freshness"]["status"], "stale")
        self.assertEqual(recalled["sources"], parent["sources"])
        self.assertFalse(recalled["qualifications"]["source_binding"]["claim_verified"])
