from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import reliability
import project_workspace
import claim_graph
import evidence_store
import acceptance_runs


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        project_workspace.project_create(self.root, "OPPA-444")

    def tearDown(self) -> None:
        self.tmp.cleanup()


    def _put_evidence(self, *, reranker_timeout: bool = False) -> str:
        retrieval = {
            "rerank_attempted": True,
            "rerank_applied": not reranker_timeout,
            "rerank_backend": "tei",
            "rerank_scores_returned_to_awoki": 30 if not reranker_timeout else 0,
            "rerank_results_requested_top_n": 30,
            "rerank_reason": "Request timed out." if reranker_timeout else "reranker returned explicit scores",
            "rerank_failure_class": "timeout" if reranker_timeout else "none",
            "rerank_retryable": bool(reranker_timeout),
            "rerank_degraded": bool(reranker_timeout),
        }
        stored = evidence_store.put(
            self.root, "OPPA-444", kind="code_search_result", tool="codebase_search",
            payload={"details": {"retrieval": retrieval}, "hits": []},
            scope_identity={"project_id": "OPPA-444", "repo_id": "demo", "revision_key": "r1"},
        )
        self.assertEqual(stored["status"], "stored")
        return str(stored["evidence_ref"])

    def test_assessment_graph_keeps_semantics_flexible_with_strict_evidence_refs(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Interpretive security finding is coherent",
            required_checks=["source review"],
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"], check_name="source review",
            status="passed", evidence="reviewed captured evidence",
        )
        ref = self._put_evidence()
        observed = reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="obs-source",
            kind="observation", statement="The stored retrieval shows the relevant implementation path.",
            status="supported", authority="tool_evidence", evidence_refs=[ref], required=False,
        )["assessment"]
        self.assertEqual(observed["evidence_refs"][0]["evidence_ref"], ref)
        inferred = reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="claim-path",
            kind="claim", statement="The request path appears to depend on that implementation before authorization.",
            analysis_summary="This is an interpretive conclusion, not a machine-verifier receipt.",
            status="supported", authority="model_inference", required=True,
            relations=[{"type": "derived_from", "target_id": "obs-source"}],
        )["assessment"]
        self.assertEqual(inferred["authority"], "model_inference")
        checkpoint = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(checkpoint["result"], "VERIFIED")
        self.assertIn("does not self-certify", checkpoint["interpretive_boundary"])
        final = reliability.finish_run(self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed")
        self.assertEqual(final["status"], "passed")

    def test_reranker_degradation_is_finding_only_when_capability_is_required(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Source finding remains usable during reranker degradation",
            required_checks=["source review"],
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"], check_name="source review",
            status="passed", evidence="reviewed",
        )
        ref = self._put_evidence(reranker_timeout=True)
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="source-finding",
            kind="observation", statement="Source evidence still establishes the implementation location.",
            status="supported", authority="tool_evidence", evidence_refs=[ref], required=True,
        )
        first = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(first["result"], "VERIFIED_WITH_FINDINGS")
        self.assertEqual(first["backend_reliability_findings"][0]["failure_class"], "timeout")
        self.assertEqual(first["required_nodes_with_degraded_backend_evidence"], [])
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="source-finding",
            kind="observation", statement="This acceptance claim specifically requires complete reranker scoring.",
            status="supported", authority="tool_evidence", evidence_refs=[ref], required=True,
            requirements=["reranker_complete"],
        )
        second = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(second["result"], "INCOMPLETE")
        self.assertEqual(second["required_nodes_with_degraded_backend_evidence"], ["source-finding"])
        self.assertEqual(second["corrective_budget_remaining"], 1)

    def test_required_assessment_must_have_current_clear_checkpoint_before_pass(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Assessment gated completion", required_checks=["source review"],
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"], check_name="source review",
            status="passed", evidence="reviewed",
        )
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="analyst-observation",
            kind="observation", statement="Analyst observed the expected local behavior.",
            status="supported", authority="analyst_observation", required=True,
        )
        blocked = reliability.finish_run(
            self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed",
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("reliability_verification_checkpoint", blocked["decision_reason"])

    def test_optional_contradiction_is_visible_but_does_not_hard_fail_unrelated_required_claim(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Optional alternative remains open", required_checks=["source review"],
        )
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="required-observation",
            kind="observation", statement="Observed configuration value.", status="supported",
            authority="analyst_observation", required=True,
        )
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="alternative-contradiction",
            kind="contradiction", statement="A non-load-bearing alternative interpretation remains unresolved.",
            status="open", authority="model_inference", required=False,
        )
        checkpoint = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(checkpoint["result"], "VERIFIED_WITH_FINDINGS")
        self.assertEqual(checkpoint["unresolved_contradictions"], ["alternative-contradiction"])
        self.assertEqual(checkpoint["required_contradictions"], [])

    def test_required_failure_cannot_finalize_as_passed(self) -> None:
        run = reliability.start_run(
            self.root,
            name="OPPA-444",
            claim="Feature is complete",
            required_checks=["unit tests", "static validation"],
        )
        reliability.record_check(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            check_name="unit tests",
            status="passed",
            command="python -m unittest",
            evidence="104 tests passed",
        )
        reliability.record_check(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            check_name="static validation",
            status="failed",
            command="python validate.py",
            evidence="exit 1",
        )
        final = reliability.finish_run(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            requested_status="passed",
        )
        self.assertEqual(final["status"], "failed")
        self.assertTrue((self.root / "workspace/projects/OPPA-444" / final["report_path"]).exists())

    def test_missing_required_check_blocks_pass(self) -> None:
        run = reliability.start_run(
            self.root,
            name="OPPA-444",
            claim="Analysis is verified",
            required_checks=["source review"],
        )
        final = reliability.finish_run(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            requested_status="passed",
        )
        self.assertEqual(final["status"], "blocked")

    def test_all_required_checks_can_pass(self) -> None:
        run = reliability.start_run(
            self.root,
            name="OPPA-444",
            claim="Analysis is verified",
            required_checks=["source review"],
        )
        reliability.record_check(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            check_name="source review",
            status="passed",
            evidence="reviewed src/auth.py lines 1-40",
        )
        final = reliability.finish_run(
            self.root,
            name="OPPA-444",
            run_id=run["run_id"],
            requested_status="passed",
        )
        self.assertEqual(final["status"], "passed")

    def test_ship_mode_requires_machine_verified_claims(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Ship verified analysis",
            required_checks=["source review"], mode="ship",
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"],
            check_name="source review", status="passed", evidence="reviewed",
        )
        no_claims = reliability.finish_run(
            self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed",
        )
        self.assertEqual(no_claims["status"], "blocked")

    def test_model_cannot_self_certify_verified_claim(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Ship verified analysis",
            required_checks=["source review"], mode="ship",
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"],
            check_name="source review", status="passed", evidence="reviewed",
        )
        recorded = reliability.record_claim(
            self.root, name="OPPA-444", run_id=run["run_id"], claim_id="C1",
            subject="Forwarded", predicate="present_after_rewrite", value=False,
            status="VERIFIED",
        )
        self.assertEqual(recorded["claims"][0]["status"], "INCONCLUSIVE")
        final = reliability.finish_run(
            self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed",
        )
        self.assertEqual(final["status"], "blocked")

    def test_machine_verified_value_conflict_fails_ship(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Ship verified analysis",
            required_checks=["source review"], mode="ship",
        )
        reliability.record_check(
            self.root, name="OPPA-444", run_id=run["run_id"],
            check_name="source review", status="passed", evidence="reviewed",
        )
        for cid, value in (("C1", False), ("C2", True)):
            verifier_result = {"status": "ok", "verdict": "VERIFIED", "observed": value}
            reliability.record_claim(
                self.root, name="OPPA-444", run_id=run["run_id"], claim_id=cid,
                repo_id="oathkeeper", subject="Forwarded", predicate="present_after_rewrite",
                value=value, status="VERIFIED", verifier=claim_graph.verifier_receipt("test", verifier_result),
            )
        final = reliability.finish_run(
            self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed",
        )
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["claim_gate"]["conflicts"][0]["type"], "value_conflict")

    def test_verifier_status_mapping_is_fail_closed(self) -> None:
        self.assertEqual(claim_graph.status_from_code_verifier({"verdict": "VERIFIED"}), "VERIFIED")
        self.assertEqual(claim_graph.status_from_code_verifier({"status": "stale_source"}), "STALE")
        self.assertEqual(claim_graph.status_from_semantics_verifier({
            "status": "ok", "semantics_class": "language", "toolchain_alignment": "unknown"
        }), "VERIFIED")
        self.assertEqual(claim_graph.status_from_semantics_verifier({
            "status": "ok", "semantics_class": "stdlib_or_runtime", "toolchain_alignment": "unknown"
        }), "INCONCLUSIVE")
        self.assertEqual(claim_graph.status_from_semantics_verifier({
            "status": "ok", "semantics_class": "stdlib_or_runtime", "toolchain_alignment": "major_minor_match"
        }), "VERIFIED")


    def test_empty_claim_gate_is_not_applicable_not_vacuously_verified(self) -> None:
        gate = claim_graph.gate([], require_claims=False)
        self.assertEqual(gate["status"], "not_applicable")
        self.assertEqual(gate["result"], "NOT_APPLICABLE")
        self.assertIn("no required structured claims", gate["reason"])

    def test_subject_contract_requires_declared_claim_ids_and_properties_are_checks(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="Contracted verification",
            subject="R9.1.6.10 verification model", required_checks=["source review"],
            required_claims=["C-required"], required_properties=["relations preserve provenance"],
        )
        self.assertIn("relations preserve provenance", [c["name"] for c in run["checks"] if c.get("required")])
        checkpoint = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(checkpoint["result"], "INCOMPLETE")
        self.assertEqual(checkpoint["claim_gate"]["status"], "blocked")
        self.assertIn("C-required", checkpoint["claim_gate"]["missing_required_claims"])

    def test_note_is_flexible_non_gating(self) -> None:
        run = reliability.start_run(self.root, name="OPPA-444", claim="Notes remain expressive", required_checks=["source review"])
        recorded = reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="note-context",
            kind="note", statement="Investigation context may remain flexible and point to richer ev artifacts.",
            analysis_summary="Non-gating context.", authority="analyst_observation", required=False,
        )
        self.assertEqual(recorded["assessment"]["kind"], "note")
        checkpoint = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(checkpoint["result"], "VERIFIED")
        with self.assertRaises(ValueError):
            reliability.record_assessment(
                self.root, name="OPPA-444", run_id=run["run_id"], kind="note",
                statement="Notes cannot silently become proof.", required=True,
            )

    def test_first_class_relation_can_be_added_after_nodes(self) -> None:
        run = reliability.start_run(self.root, name="OPPA-444", claim="Relations are first class", required_checks=["source review"])
        ref = self._put_evidence()
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="runtime-observation",
            kind="observation", statement="Runtime trace observed the selected path.", status="supported",
            authority="runtime_observation", evidence_refs=[ref], required=False,
        )
        reliability.record_assessment(
            self.root, name="OPPA-444", run_id=run["run_id"], node_id="inference",
            kind="claim", statement="The path is likely selected for this request class.", status="supported",
            authority="model_inference", required=True,
        )
        rel = reliability.record_relation(
            self.root, name="OPPA-444", run_id=run["run_id"], from_node_id="runtime-observation",
            relation_type="supports", to_node_id="inference",
        )
        self.assertTrue(rel["relation"]["relation_id"].startswith("rel_"))
        checkpoint = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(checkpoint["result"], "VERIFIED")
        self.assertEqual(checkpoint["relation_count"], 1)

    def test_corrective_budget_is_atomic_and_checkpoint_does_not_consume_it(self) -> None:
        run = reliability.start_run(
            self.root, name="OPPA-444", claim="One bounded correction", required_checks=["source review"], corrective_budget=1,
        )
        first = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(first["corrective_budget_remaining"], 1)
        consumed = reliability.consume_corrective_budget(
            self.root, name="OPPA-444", run_id=run["run_id"], action="one targeted source verification",
        )
        self.assertEqual(consumed["corrective_budget"]["remaining"], 0)
        second = reliability.verification_checkpoint(self.root, name="OPPA-444", run_id=run["run_id"])
        self.assertEqual(second["corrective_budget_remaining"], 0)
        rejected = reliability.consume_corrective_budget(
            self.root, name="OPPA-444", run_id=run["run_id"], action="second correction",
        )
        self.assertEqual(rejected["status"], "rejected")

    def test_acceptance_observations_cannot_shadow_corrective_budget(self) -> None:
        acceptance = acceptance_runs.start(
            self.root, project_id="OPPA-444", suite="budget-shadow", expected_tests=["TEST1"], expected_invariants=[],
        )
        result = acceptance_runs.record(
            self.root, run_id=acceptance["run_id"], project_id="OPPA-444", test_id="TEST1", outcome="pass",
            evidence={"corrective_budget_remaining": 1, "observation": "otherwise compact"},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "acceptance_evidence_invalid")

    def test_cross_ledger_aggregation_preserves_component_scope(self) -> None:
        run = reliability.start_run(self.root, name="OPPA-444", claim="Mechanics pass", required_checks=["mechanics"])
        reliability.record_check(self.root, name="OPPA-444", run_id=run["run_id"], check_name="mechanics", status="passed", evidence="ok")
        final = reliability.finish_run(self.root, name="OPPA-444", run_id=run["run_id"], requested_status="passed")
        self.assertEqual(final["status"], "passed")
        acceptance = acceptance_runs.start(
            self.root, project_id="OPPA-444", suite="verification-aggregation",
            expected_tests=["TEST1"], expected_invariants=[],
        )
        acceptance_runs.record(
            self.root, run_id=acceptance["run_id"], project_id="OPPA-444", test_id="TEST1", outcome="fail", evidence={"reason": "precondition blocked"},
        )
        acceptance_runs.finalize(self.root, run_id=acceptance["run_id"], project_id="OPPA-444")
        aggregate = reliability.aggregate_verdict(
            self.root, name="OPPA-444", reliability_run_id=run["run_id"], acceptance_run_id=acceptance["run_id"],
        )
        self.assertEqual(aggregate["components"]["reliability"]["verdict"], "PASSED")
        self.assertEqual(aggregate["components"]["acceptance"]["verdict"], "NOT_PASSED")
        self.assertEqual(aggregate["overall_verdict"], "NOT_PASSED")

if __name__ == "__main__":
    unittest.main()
