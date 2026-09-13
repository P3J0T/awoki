from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import claim_graph
import findings_review
import memory_recall
import project_workspace
import reliability
import evidence_store


class FindingsReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        project_workspace.project_create(self.root, "review-demo")
        self.run = reliability.start_run(self.root, name="review-demo", claim="Review requested findings",
                                         required_checks=["review sources"], mode="verify")
        self.args = {"run_id": self.run["run_id"], "name": "review-demo"}

    def code_claim(self, **overrides):
        result = {"status": "validated", "verdict": "VERIFIED", "claim": "entry calls guard",
                  "source": {"project_id": "review-demo", "repo_id": "demo", "commit_sha": "abc"},
                  "certainty_boundary": "Direct source call, not execution or result consumption."}
        return {"claim_id": "c1", "status": "VERIFIED", "required": True,
                "subject": "all endpoints", "predicate": "secure", "value": True,
                "verifier": claim_graph.verifier_receipt("code_validate_claim", result), **overrides}

    def test_receipt_preserves_actual_claim_not_arbitrary_label(self):
        item = findings_review.review({"claims": [self.code_claim()]})["items"][0]
        self.assertEqual(item["label"], "MACHINE_CHECKED")
        self.assertEqual(item["checked_scope"]["claim"], "entry calls guard")
        self.assertEqual(item["analyst_claim"]["predicate"], "secure")
        self.assertFalse(item["interpretation_verified"])
        self.assertEqual(item["source_freshness"], "not_rechecked")

    def test_legacy_or_mismatched_receipt_cannot_receive_checked_label(self):
        for change in ({"schema": 1}, {"checked": {}}, {"verdict": "INCONCLUSIVE"}, {"kind": "model"}):
            row = self.code_claim()
            row["verifier"].update(change)
            self.assertEqual(findings_review.review({"claims": [row]})["items"][0]["label"], "UNKNOWN")

    def test_refutation_only_applies_to_exact_checked_scope(self):
        row = self.code_claim(status="REFUTED")
        row["verifier"]["verdict"] = "REFUTED"
        item = findings_review.review({"claims": [row]})["items"][0]
        self.assertEqual(item["label"], "MACHINE_REFUTED")
        self.assertFalse(item["interpretation_verified"])

    def test_semantics_requires_inputs_observation_and_alignment(self):
        result = {"status": "ok", "language": "go", "operation": "path_join", "inputs": {"parts": ["a", "b"]},
                  "observed": "a/b", "semantics_class": "stdlib", "toolchain_alignment": "major_minor_match"}
        row = self.code_claim(verifier=claim_graph.verifier_receipt("code_semantics_check", result))
        self.assertEqual(findings_review.review({"claims": [row]})["items"][0]["label"], "MACHINE_CHECKED")
        for key in ("inputs", "observed", "toolchain_alignment"):
            edited = json.loads(json.dumps(row))
            edited["verifier"]["checked"].pop(key)
            self.assertEqual(findings_review.review({"claims": [edited]})["items"][0]["label"], "UNKNOWN")

    def test_assessment_checkpoint_does_not_certify_interpretation(self):
        ref = evidence_store.put(self.root, "review-demo", kind="observation", tool="test",
                                  payload={"text": "source observed"}, scope_identity={"project_id": "review-demo"})["evidence_ref"]
        reliability.record_assessment(self.root, **self.args, node_id="interpretation", kind="claim",
            statement="all endpoints are secure", authority="model_inference", status="supported",
            evidence_refs=[ref], required=True)
        checkpoint = reliability.verification_checkpoint(self.root, **self.args)
        self.assertEqual(checkpoint["result"], "VERIFIED")
        review = reliability.get_run(self.root, **self.args, view="review")
        self.assertEqual(review["items"][0]["label"], "EVIDENCE_ATTACHED_UNVERIFIED")
        self.assertFalse(review["items"][0]["interpretation_verified"])

    def test_unknowns_and_contradictions_remain_explicit(self):
        rows = [{"node_id": "missing", "kind": "gap", "status": "open", "required": True},
                {"node_id": "conflict", "kind": "contradiction", "status": "supported"}]
        review = findings_review.review({"assessments": rows})
        self.assertEqual([r["label"] for r in review["items"]], ["UNKNOWN", "CONTRADICTED"])
        self.assertIn("omitted claims", review["boundary"])

    def test_pagination_preserves_total_unknowns_and_hash_changes(self):
        run = {"run_id": "run1", "project_id": "review-demo", "assessments": [
            {"node_id": str(i), "statement": "unknown", "kind": "gap"} for i in range(14)]}
        review = findings_review.review(run)
        self.assertFalse(review["complete"])
        self.assertEqual(review["counts"], {"UNKNOWN": 14})
        self.assertEqual(review["next_call"]["arguments"]["offset"], 12)
        self.assertEqual(len(findings_review.review(run, offset=12)["items"]), 2)
        run["assessments"][0]["statement"] = "changed"
        self.assertNotEqual(review["review_sha256"], findings_review.review(run)["review_sha256"])

    def test_status_is_read_only_and_checks_project_identity(self):
        path = self.root / "workspace/projects/review-demo" / self.run["json_path"]
        before = path.read_bytes(), path.stat().st_mtime_ns
        reliability.get_run(self.root, **self.args, view="review")
        reliability.get_run(self.root, **self.args)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        value = json.loads(path.read_text())
        value["project_id"] = "other"
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            reliability.get_run(self.root, **self.args, view="review")

    def test_invalid_run_paths_rejected(self):
        for run_id in ("../secret", "../../other", "/tmp/x", "a/b", "..", "x\\y"):
            with self.assertRaises(ValueError):
                reliability.get_run(self.root, run_id=run_id, name="review-demo", view="review")
        for kwargs in ({"offset": -1}, {"limit": 0}, {"limit": 51}):
            with self.assertRaises(ValueError):
                findings_review.review(self.run, **kwargs)

    def test_finish_report_reference_and_caveats_survive_followup(self):
        final = reliability.finish_run(self.root, **self.args, requested_status="reliably-paused")
        self.assertNotIn("continuity_capture_warning", final)
        actual = project_workspace.continuity_records(project_workspace.paths_for(self.root, "review-demo"))[-1]
        self.assertIn(findings_review.BOUNDARY, actual["uncertainty"])
        parent = {"id": "cont_test", "summary": "Review", "confidence": "medium",
                  "sources": [{"type": "file", "path": final["report_path"]}],
                  "uncertainty": [findings_review.BOUNDARY]}
        inherited = memory_recall.inherit_qualifications({"cont_test": parent}, ["cont_test"], [], [], "high")
        child = {**inherited, "id": "cont_child", "summary": "A stronger paraphrase"}
        projected = memory_recall.project_record(child, "review-demo", section="details")
        link = projected["qualifications"]["findings_reviews"][0]
        self.assertEqual(link["arguments"]["run_id"], self.run["run_id"])
        self.assertFalse(link["claim_verified"])
        self.assertEqual(child["confidence"], "medium")
        self.assertIn(findings_review.BOUNDARY, child["uncertainty"])

    def test_unrelated_notes_have_no_review_overhead(self):
        projected = memory_recall.project_record({"id": "cont_x", "sources": []}, "review-demo")
        self.assertNotIn("findings_reviews", projected["qualifications"])
        self.assertEqual(findings_review.memory_links([{"type": "file", "path": "reports/reliability/../../secret.md"}], "review-demo"), [])

    def test_markdown_does_not_label_analyst_wording_verified(self):
        run = {**self.run, "claims": [self.code_claim()],
               "latest_verification_checkpoint": {"result": "VERIFIED"}}
        report = reliability._render_markdown(run)
        self.assertIn("Analyst wording (not certified)", report)
        self.assertIn("entry calls guard", report)
        self.assertIn("not semantic proof", report)
        self.assertNotIn("**VERIFIED** (project)", report)


if __name__ == "__main__":
    unittest.main()
