"""The post-summary bridge is bounded, read-only navigation, never a second plan."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import acceptance_runs
import opencode_events as events
import project_workspace as workspace
import work_ledger


class RecoveryContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sid = "native-session-one"
        workspace.project_create(self.root, "demo", session_id=self.sid)
        self.pp = workspace.paths_for(self.root, "demo")

    def save(self, summary, **kwargs):
        return workspace.project_capture(self.root, "demo", summary, refresh=False, sync_index=False, **kwargs)

    def recovery(self, **kwargs):
        result = events.recovery_context(self.root, self.sid, **kwargs)
        self.assertEqual(result["session_id"], self.sid)
        self.assertLessEqual(len(result["context"]), 2500)
        return result

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_read_only_saved_goal_exact_calls_and_no_task_prose(self):
        goal = self.save("Check empty-cart behavior", kind="direction", details="Private full detail not for prompt")
        checkpoint = self.save("Inspect callback next", kind="finding", state="checkpoint")
        work_ledger.sync_todos(self.root, self.sid, [{"content": f"RAW TODO PROSE {goal['id']}", "status": "pending"}])
        before = self.files()
        result = self.recovery()
        self.assertEqual(self.files(), before)
        context = result["context"]
        self.assertEqual(result["status"], "ok")
        self.assertIn("saved_direction: saved", context)
        self.assertIn(goal["id"], context)
        self.assertIn(checkpoint["id"], context)
        self.assertIn('"record_ids":[', context)
        self.assertIn("pending=1", context)
        for forbidden in ("RAW TODO PROSE", "Private full detail", "Awoki execution invariants", "HANDOFF"):
            self.assertNotIn(forbidden, context)

    def test_private_retired_closed_and_foreign_reference_hints_are_omitted(self):
        old = self.save("OLD LABEL", kind="observation")
        public = self.save("PUBLIC LABEL", kind="correction", supersedes=[old["id"]])
        private = self.save("PRIVATE LABEL", sensitivity="secret")
        no_rag = self.save("NO RAG LABEL", index_policy="no_rag")
        closed = self.save("CLOSED LABEL", kind="checkpoint", state="closed")
        hidden = [old, private, no_rag, closed]
        work_ledger.sync_todos(self.root, self.sid, [{"content": " ".join(note["id"] for note in [*hidden, public]), "status": "pending"}])
        for note in hidden:
            work_ledger.touch_reference(self.root, self.sid, project_id="demo", reference_id=note["id"], label="STALE PRIVATE LABEL")
        work_ledger.touch_reference(self.root, self.sid, project_id="other", reference_id="cont_foreign_secret", label="FOREIGN LABEL")
        context = self.recovery()["context"]
        self.assertIn(public["id"], context)
        for forbidden in [*(note["id"] for note in hidden), "PRIVATE LABEL", "NO RAG", "STALE", "FOREIGN", "cont_foreign_secret"]:
            self.assertNotIn(forbidden, context)

    def test_corrupt_canonical_journal_is_unknown_not_empty_or_stale(self):
        note = self.save("Current direction", kind="direction")
        work_ledger.sync_todos(self.root, self.sid, [{"content": note["id"], "status": "pending"}])
        self.pp.continuity.write_text("{broken\n")
        context = self.recovery()["context"]
        self.assertIn("saved_direction: unknown", context)
        self.assertNotIn(note["id"], context)
        self.assertNotIn("No saved direction", context)
        self.assertIn("pending=1", context)

    def test_corrupt_or_legacy_work_is_unknown_and_never_migrated(self):
        goal = self.save("Saved goal", kind="direction")
        path = work_ledger._path(self.root, self.sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        for raw in ("{bad", json.dumps({**work_ledger._base(self.sid), "schema": "awoki-session-work/v1"})):
            with self.subTest(raw=raw):
                path.write_text(raw)
                before = self.files()
                result = self.recovery()
                self.assertEqual(self.files(), before)
                self.assertIn("work: unknown", result["context"])
                self.assertIn("TODO counts/review/omissions unknown", result["context"])
                self.assertIn(goal["id"], result["context"])

    def test_unattached_is_valid_exploration_not_completion(self):
        workspace.detach_session_project(self.root, session_id=self.sid)
        result = self.recovery()
        self.assertEqual(result["status"], "ok")
        self.assertIn("scope: unattached", result["context"])
        self.assertIn("saved_direction: none", result["context"])
        self.assertIn("not proof of completion", result["context"])
        self.assertNotIn('project="demo"', result["context"])

    def test_no_saved_goal_does_not_discard_current_request_or_prior_source_work(self):
        result = self.recovery()
        context = result["context"]
        self.assertIn("saved_direction: none", context)
        self.assertIn("No SAVED direction", context)
        self.assertIn("Continue the current user request using available evidence", context)
        self.assertIn("does not mean no current task or erase prior source work", context)
        self.assertIn("Do not ask for a new task solely because no goal note exists", context)
        self.assertIn("no invented goal", context)
        self.assertIn("newest user request takes precedence", context)
        self.assertNotIn("next_call: project_search", context)

    def test_corrupt_scope_and_midread_scope_switch_fail_closed(self):
        path = workspace.session_state_path(self.root, self.sid)
        original = path.read_bytes()
        path.write_text("{bad")
        self.assertEqual(self.recovery()["status"], "unknown")
        path.write_bytes(original)
        with patch.object(workspace, "session_attachment_status", side_effect=[
            {"status": "attached", "project_id": "demo"}, {"status": "unattached", "project_id": ""}
        ]):
            result = self.recovery()
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("demo", result["context"])

    def test_scope_mismatch_does_not_restore_previous_project_refs(self):
        goal = self.save("Prior project goal", kind="direction")
        work_ledger.sync_todos(self.root, self.sid, [{"content": goal["id"], "status": "pending"}])
        workspace.project_create(self.root, "other", session_id=self.sid)
        result = self.recovery()
        self.assertIn('project="other"', result["context"])
        self.assertIn("todo_scope_matches: false", result["context"])
        self.assertIn("review=true", result["context"])
        self.assertNotIn(goal["id"], result["context"])

    def test_current_completed_finding_is_valid_knowledge_but_old_turn_refs_are_not_active(self):
        completed = self.save("Completed evidence-backed finding", kind="finding", state="completed")
        previous = self.save("Earlier turn observation")
        work_ledger.touch_reference(self.root, self.sid, project_id="demo", reference_id=previous["id"], label="OLD TURN LABEL")
        work_ledger.mark_user_turn(self.root, self.sid, message_id="new-turn")
        work_ledger.touch_reference(self.root, self.sid, project_id="demo", reference_id=completed["id"], label="CURRENT FINDING LABEL")
        result = self.recovery()
        self.assertIn(completed["id"], result["context"])
        self.assertNotIn(previous["id"], result["context"])
        self.assertIn("reference_review=true", result["context"])
        self.assertNotIn("OLD TURN LABEL", result["context"])

    def test_future_reference_generation_is_unknown_metadata(self):
        note = self.save("Finding")
        work_ledger.touch_reference(self.root, self.sid, project_id="demo", reference_id=note["id"])
        path = work_ledger._path(self.root, self.sid)
        work = json.loads(path.read_text())
        work["active_references"][0]["user_turn_generation"] = work["user_turn_generation"] + 1
        path.write_text(json.dumps(work))
        result = self.recovery()
        self.assertIn("work: unknown", result["context"])
        self.assertNotIn(note["id"], result["context"])

    def seed_acceptance(self, **changes):
        run_id = "acr_1234567890abcdef"
        state = {"schema": acceptance_runs.SCHEMA, "run_id": run_id, "project_id": "demo",
                 "origin_session_key": acceptance_runs._session_key(self.sid), "status": "running", **changes}
        acceptance_runs._write(acceptance_runs._path(self.root, "demo", run_id), state)
        acceptance_runs._write(acceptance_runs._session_path(self.root, self.sid), {"run_id": run_id, "project_id": "demo"})
        return run_id

    def test_active_acceptance_overrides_generic_recovery_and_does_not_migrate(self):
        goal = self.save("General investigation", kind="direction")
        run_id = self.seed_acceptance()
        before = self.files()
        result = self.recovery()
        self.assertEqual(self.files(), before)
        self.assertEqual(result["acceptance_run_id"], run_id)
        self.assertIn("acceptance_run_next()", result["context"])
        self.assertNotIn("project_search", result["context"])
        self.assertNotIn("session_work_status", result["context"])
        self.assertNotIn(goal["id"], result["context"])

    def test_unknown_acceptance_never_authorizes_generic_recovery(self):
        for change in ({"schema": "legacy"}, {"project_id": "foreign"}, {"origin_session_key": "other"}):
            with self.subTest(change=change):
                self.seed_acceptance(**change)
                result = self.recovery()
                self.assertEqual(result["status"], "unknown")
                self.assertIn("unrestricted actions", result["context"])
                self.assertNotIn("project_search", result["context"])
                self.assertNotIn("acr_", result["context"])
        acceptance_runs._session_path(self.root, self.sid).write_text("{corrupt")
        self.assertEqual(self.recovery()["status"], "unknown")

    def test_acceptance_activated_during_recovery_invalidates_generic_snapshot(self):
        with patch.object(events, "_recovery_acceptance", side_effect=[
            {"status": "none"}, {"status": "active", "run_id": "acr_new", "project_id": "demo"}
        ]):
            result = self.recovery()
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("project_search", result["context"])

    def test_private_direction_keeps_recovery_unknown_without_its_identity(self):
        private = self.save("RESTRICTED DIRECTION LABEL", kind="direction", sensitivity="secret")
        work_ledger.sync_todos(self.root, self.sid, [{"content": private["id"], "status": "pending"}])
        context = self.recovery()["context"]
        self.assertIn("saved_direction: unknown", context)
        self.assertNotIn(private["id"], context)
        self.assertNotIn("RESTRICTED", context)

    def test_small_budget_keeps_whole_lines_and_explicit_omission(self):
        for index in range(6):
            self.save(f"Direction {index}", kind="direction")
        result = self.recovery(max_chars=600)
        self.assertLessEqual(len(result["context"]), 600)
        self.assertIn("omitted", result["context"])
        self.assertIn("saved_direction: ambiguous", result["context"])

    def test_omission_and_review_flags_survive_without_todo_prose(self):
        note = self.save("Normal finding")
        work_ledger.sync_todos(self.root, self.sid, [{"content": "SECRET TASK PROSE " * 60 + note["id"], "status": "pending"}] * 65)
        work_ledger.touch_reference(self.root, self.sid, project_id="demo", reference_id=note["id"], label="STALE LABEL")
        work_ledger.mark_user_turn(self.root, self.sid, message_id="new-user")
        context = self.recovery()["context"]
        self.assertIn("pending=64", context)
        self.assertIn("omitted=1", context)
        self.assertIn("shortened=64", context)
        self.assertIn("reference_review=true", context)
        self.assertIn(note["id"], context)
        self.assertNotIn("SECRET TASK PROSE", context)
        self.assertNotIn("STALE LABEL", context)

    def test_ambiguous_candidates_bounded_and_exact_calls_have_at_most_three_ids(self):
        for index in range(7):
            self.save(f"Goal {index}", kind="direction")
        for index in range(6):
            self.save(f"Checkpoint {index}", kind="checkpoint")
        result = self.recovery(max_chars=1300)
        self.assertLessEqual(len(result["context"]), 1300)
        self.assertIn("saved_direction: ambiguous", result["context"])
        self.assertIn("omitted", result["context"])
        for line in result["context"].splitlines():
            if line.startswith("next_call: project_search("):
                args = json.loads(line[len("next_call: project_search("):-1])
                self.assertLessEqual(len(args["record_ids"]), 3)

    def test_invalid_work_metadata_is_unknown_not_guessed(self):
        work_ledger.sync_todos(self.root, self.sid, [{"content": "Ordinary task", "status": "pending"}])
        path = work_ledger._path(self.root, self.sid)
        saved = json.loads(path.read_text())
        for changes in ({"todos_need_review": "false"}, {"project_id": []}, {"session_key": "wrong"}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**saved, **changes}))
                self.assertIn("work: unknown", self.recovery()["context"])

    def test_cli_outputs_session_bound_contract(self):
        paths = events.HarnessPaths(root=self.root, global_root=self.root / ".global")
        output = io.StringIO()
        with patch.object(events.HarnessPaths, "from_env", return_value=paths), patch("sys.stdout", output):
            self.assertEqual(events.main(["recovery-context", "--session-id", self.sid]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["session_id"], self.sid)
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
