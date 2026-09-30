"""Recovery uses exact canonical notes without turning exploration into a task."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import continuity
import project_workspace as workspace


class GoalProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workspace.project_create(self.root, "demo", session_id="one")
        self.pp = workspace.paths_for(self.root, "demo")

    def save(self, summary, **kwargs):
        return workspace.project_capture(self.root, "demo", summary, refresh=False, sync_index=False, **kwargs)

    def projection(self):
        return continuity.load_goal_projection(self.pp.memory_dir, "demo")

    def fingerprint(self):
        return continuity.direction_fingerprint(self.pp.memory_dir, "demo")

    def test_exploration_and_checkpoints_do_not_invent_a_goal(self):
        self.save("Interesting branch", kind="observation")
        checkpoint = self.save("Checkpoint: callback behavior remains unknown", kind="reflection",
                               tags=["investigation-checkpoint"], details="Inspect the callback result next.")
        result = self.projection()
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["directions"], [])
        self.assertEqual(result["checkpoints"][0]["record_id"], checkpoint["id"])
        self.assertNotIn("Inspect the callback result next.", json.dumps(result))
        self.assertEqual(result["next_calls"][0]["arguments"]["record_ids"], [checkpoint["id"]])

    def test_finding_explicitly_saved_as_checkpoint_has_exact_recovery_link(self):
        checkpoint = self.save("Empty cart shipping needs callback inspection", kind="finding", state="checkpoint",
                               details="Stay within checkout.py. Inspect the shipping callback before concluding.")
        result = self.projection()
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["directions"], [])
        self.assertEqual(result["checkpoints"][0]["record_id"], checkpoint["id"])
        self.assertEqual(result["checkpoints"][0]["kind"], "finding")
        self.assertTrue(result["checkpoints"][0]["details_available"])
        self.assertEqual(result["next_calls"][0]["arguments"]["record_ids"], [checkpoint["id"]])
        workspace.refresh_project_files(self.root, "demo")
        self.assertIn(checkpoint["id"], self.pp.handoff.read_text())
        self.assertNotIn(checkpoint["details"], json.dumps(result))

    def test_checkpoint_state_still_excludes_private_retired_and_closed_notes(self):
        private = self.save("Private checkpoint label", kind="finding", state="checkpoint", sensitivity="secret")
        no_rag = self.save("Excluded checkpoint label", kind="finding", state="checkpoint", index_policy="no_rag")
        retired = self.save("Old checkpoint", kind="finding", state="checkpoint")
        closed = self.save("Closed checkpoint", kind="finding", state="closed",
                           supersedes=[retired["id"]], tags=["investigation-checkpoint"])
        current = self.save("Current checkpoint", kind="finding", state="checkpoint")
        result = self.projection()
        self.assertEqual([row["record_id"] for row in result["checkpoints"]], [current["id"]])
        self.assertEqual(result["next_calls"][0]["arguments"]["record_ids"], [current["id"]])
        rendered = json.dumps(result) + continuity.render_goal_projection(result)
        for note in (private, no_rag, retired, closed):
            self.assertNotIn(note["id"], rendered)
            self.assertNotIn(note["summary"], rendered)

    def test_checkpoint_words_without_structured_marker_do_not_create_navigation(self):
        self.save("Checkpoint: shipping investigation", kind="finding",
                  details="A checkpoint mentioned in prose alone is not explicit saved checkpoint state.")
        result = self.projection()
        self.assertEqual(result["status"], "none")
        self.assertEqual(result["checkpoints"], [])
        self.assertEqual(result["directions"], [])
        self.assertEqual(result["next_calls"], [])

    def test_goal_reference_survives_many_later_notes_and_bounded_handoff(self):
        goal = self.save("Review callback order; report unresolved behavior", kind="direction",
                         details="Do not execute the target. Completion needs a source-backed callback order.")
        for index in range(90):
            self.save(f"Later observation {index}: " + "x" * 400, details="observation detail")
        resumed = workspace.project_open(self.root, "demo", session_id="two")
        self.assertEqual(resumed["goal_recovery"]["directions"][0]["record_id"], goal["id"])
        for path, limit in ((self.pp.handoff, 32000), (self.pp.situation, 12000)):
            text = path.read_text()
            self.assertLessEqual(len(text), limit)
            self.assertIn(goal["id"], text[:1500])
            self.assertIn("details and caveats are not reproduced", text)

    def test_explicit_direction_revision_keeps_only_current_reference(self):
        old = self.save("Review authorization", kind="direction")
        new = self.save("Review callbacks instead", kind="direction", supersedes=[old["id"]])
        result = self.projection()
        self.assertEqual(result["status"], "saved")
        self.assertEqual([row["record_id"] for row in result["directions"]], [new["id"]])
        self.assertNotIn(old["id"], json.dumps(result))

    def test_competing_directions_have_no_selected_default(self):
        one = self.save("Review authorization", kind="direction")
        two = self.save("Review callbacks", kind="direction")
        result = self.projection()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual({row["record_id"] for row in result["directions"]}, {one["id"], two["id"]})
        resumed = workspace.project_resume(self.root, "demo", session_id="two")
        self.assertIn("Reconcile saved direction", resumed["suggested_next_action"])

    def test_factual_correction_of_direction_does_not_revive_older_goal(self):
        self.save("An older separate investigation", kind="direction")
        current = self.save("Review authorization", kind="direction")
        fix = self.save("Callback belongs to the other branch", kind="correction", supersedes=[current["id"]])
        result = self.projection()
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["reason"], "direction_revision_changed_kind")
        self.assertEqual(result["unresolved_revisions"][0]["record_id"], fix["id"])
        self.assertEqual(fix["kind"], "correction")

    def test_private_direction_and_checkpoint_labels_never_appear(self):
        previous = self.save("Public direction", kind="direction")
        private = self.save("Restricted customer mission", kind="direction", supersedes=[previous["id"]],
                            sensitivity="secret", index_policy="no_rag")
        checkpoint = self.save("Restricted checkpoint title", kind="checkpoint", index_policy="no_rag")
        result = self.projection()
        self.assertEqual(result["status"], "unknown")
        rendered = json.dumps(result) + continuity.render_goal_projection(result)
        for forbidden in ("Restricted", private["id"], checkpoint["id"], previous["id"]):
            self.assertNotIn(forbidden, rendered)
        workspace.refresh_project_files(self.root, "demo")
        self.assertNotIn("Restricted", self.pp.handoff.read_text())

    def test_closed_direction_is_not_an_active_goal(self):
        for state in ("done", "complete", "completed", "closed", "resolved", "superseded", "retired", "cancelled", "canceled"):
            with self.subTest(state=state):
                old = self.save("Review authorization", kind="direction")
                closed = self.save("Review complete", kind="direction", state=state, supersedes=[old["id"]])
                result = self.projection()
                self.assertEqual(result["status"], "none")
                self.assertEqual(result["directions"], [])
                self.assertEqual(result["next_calls"], [])
                self.assertNotIn(closed["id"], continuity.render_goal_projection(result))

    def test_missing_journal_stays_unknown_during_resume(self):
        self.pp.continuity.unlink()
        result = workspace.project_resume(self.root, "demo", session_id="two")
        self.assertEqual(result["goal_recovery"]["status"], "unknown")
        self.assertFalse(self.pp.continuity.exists())
        self.assertIn("recovery is incomplete", result["handoff"])
        self.assertEqual(self.fingerprint(), {"status": "unknown"})

    def test_corrupt_or_wrong_scope_journal_is_unknown_not_empty(self):
        invalid = ["{bad", "[]", json.dumps({"id": "cont_x", "project_id": "other"}),
                   json.dumps({"id": "cont_x", "project_id": "demo", "supersedes": "cont_old"}),
                   json.dumps({"id": "cont_x", "project_id": "demo", "kind": "direction", "summary": "A goal", "metadata": ["bad"]})]
        for value in invalid:
            with self.subTest(value=value):
                self.pp.continuity.write_text(value + "\n")
                self.assertEqual(self.projection()["status"], "unknown")
                self.assertEqual(self.fingerprint(), {"status": "unknown"})
                self.assertEqual(self.projection()["reason"], "canonical_state_unavailable")

    def test_read_error_is_unknown_without_error_content(self):
        with patch.object(continuity, "_read_goal_records", side_effect=PermissionError("private path")):
            self.assertEqual(self.fingerprint(), {"status": "unknown"})
            self.assertEqual(self.projection()["status"], "unknown")
            self.assertNotIn("private path", json.dumps(self.projection()))

    @unittest.skipIf(continuity.fcntl is None, "Shared file locks unavailable")
    def test_reader_waits_for_complete_concurrent_append(self):
        record = continuity.make_record("demo", "Concurrent saved direction", kind="direction")
        encoded = json.dumps(record) + "\n"
        started = threading.Event()
        completed = threading.Event()

        def read():
            started.set()
            result = self.projection()
            completed.set()
            return result

        with self.pp.continuity.open("a", encoding="utf-8") as writer, ThreadPoolExecutor(max_workers=1) as executor:
            continuity.fcntl.flock(writer.fileno(), continuity.fcntl.LOCK_EX)
            try:
                writer.write(encoded[:len(encoded) // 2])
                writer.flush()
                future = executor.submit(read)
                self.assertTrue(started.wait(2))
                self.assertFalse(completed.wait(0.1))
                writer.write(encoded[len(encoded) // 2:])
                writer.flush()
            finally:
                continuity.fcntl.flock(writer.fileno(), continuity.fcntl.LOCK_UN)
            result = future.result(timeout=2)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["directions"][0]["record_id"], record["id"])

    def test_cyclic_supersession_is_unknown_instead_of_no_goal(self):
        one = continuity.make_record("demo", "First direction", kind="direction", record_id="cont_one", supersedes=["cont_two"])
        two = continuity.make_record("demo", "Second direction", kind="direction", record_id="cont_two", supersedes=["cont_one"])
        self.pp.continuity.write_text("\n".join(json.dumps(row) for row in (one, two)) + "\n")
        self.assertEqual(self.projection()["status"], "unknown")
        self.assertEqual(self.fingerprint(), {"status": "unknown"})

    def test_verified_empty_journal_is_a_known_empty_direction_set(self):
        self.pp.continuity.write_text("")
        self.assertEqual(self.projection()["status"], "none")
        self.assertEqual(self.fingerprint()["status"], "known")

    def test_empty_direction_does_not_repeat_empty_sections_in_generated_views(self):
        workspace.refresh_project_files(self.root, "demo")
        for path in (self.pp.situation, self.pp.handoff):
            self.assertNotIn("## Saved direction and checkpoint details", path.read_text())
        self.assertEqual(workspace.project_open(self.root, "demo", session_id="two")["goal_recovery"]["status"], "none")

    def test_fingerprint_tracks_private_and_transitive_direction_changes_only(self):
        initial = self.fingerprint()
        self.save("A source observation")
        self.assertEqual(self.fingerprint(), initial)
        direction = self.save("Restricted direction", kind="direction", sensitivity="secret")
        one = self.fingerprint()
        self.assertNotEqual(one, initial)
        correction = self.save("Updated restriction", kind="correction", supersedes=[direction["id"]], sensitivity="secret")
        two = self.fingerprint()
        self.assertNotEqual(two, one)
        self.save("Stop this investigation", kind="correction", state="closed", supersedes=[correction["id"]])
        self.assertNotEqual(self.fingerprint(), two)
        self.assertNotIn("Restricted", json.dumps(one))
        self.assertEqual(len(one["fingerprint"]), 64)

    def test_projection_budget_omits_whole_records_and_keeps_exact_refs(self):
        saved = [self.save(f"Direction {index}: " + "x" * 1500, kind="direction") for index in range(8)]
        result = self.projection()
        self.assertEqual(result["omitted"]["directions"], 5)
        rendered = continuity.render_goal_projection(result, max_chars=650)
        self.assertLessEqual(len(rendered), 650)
        self.assertIn("omitted", rendered)
        self.assertIn(saved[-1]["id"], rendered)
        self.assertIn("no single goal is selected", rendered)
        self.assertNotIn("x" * 241, rendered)

    def test_checkpoint_details_use_note_ids_separate_from_source_evidence(self):
        goal = self.save("Inspect callback order", kind="direction", sources=["ev_example_source"],
                         details="Read source evidence ev_example_source.")
        result = self.projection()
        self.assertEqual(result["next_calls"][0]["arguments"]["record_ids"], [goal["id"]])
        self.assertNotIn("ev_example_source", json.dumps(result))

    def test_mixed_recovery_references_use_executable_three_id_batches(self):
        import harness_core as core

        directions = [self.save(f"Active direction {index}", kind="direction") for index in range(3)]
        checkpoints = [self.save(f"Checkpoint {index}", kind="checkpoint") for index in range(2)]
        revisions = []
        for index in range(2):
            previous = self.save(f"Previous direction {index}", kind="direction")
            revisions.append(self.save(f"Direction needs reconciliation {index}", kind="correction",
                                       supersedes=[previous["id"]]))
        result = self.projection()
        expected = [row["id"] for group in (directions, checkpoints, revisions) for row in reversed(group)]
        calls = result["next_calls"]
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual([len(call["arguments"]["record_ids"]) for call in calls], [3, 3, 1])
        self.assertEqual([ref for call in calls for ref in call["arguments"]["record_ids"]], expected)
        self.assertIn("up to 3 IDs", "\n".join(continuity.goal_projection_lines(result)))
        paths = core.HarnessPaths(self.root, self.root / "global")
        recovered = []
        for call in calls:
            self.assertEqual(call["tool"], "project_search")
            response = core.project_search(**call["arguments"], paths=paths)
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["unavailable_ids"], [])
            recovered.extend(row["id"] for row in response["records"])
        self.assertEqual(recovered, expected)


if __name__ == "__main__":
    unittest.main()
