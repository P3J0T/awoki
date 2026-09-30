"""Detached readiness must not resume an obsolete saved investigation direction."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuations
import project_workspace


class ContinuationDirectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = "direction-session"
        project_workspace.project_create(self.root, "demo", session_id=self.sid)

    def capture(self, summary: str, **kwargs):
        return project_workspace.project_capture(
            self.root, "demo", summary, refresh=False, sync_index=False, **kwargs,
        )

    def schedule(self, **kwargs):
        return continuations.schedule(
            self.root, self.sid, workflow="repository-readiness", phase="prepare",
            wait_tool="repository_prepare_status", wait_job_id="rpr_fixture",
            wait_seconds=2, project_id="demo", resume_goal="Inspect the requested flow",
            **kwargs,
        )

    def update_record(self, **changes):
        path = project_workspace.session_state_path(self.root, self.sid)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["continuation"].update(changes)
        path.write_text(json.dumps(state), encoding="utf-8")

    def make_ready(self):
        self.update_record(not_before="2000-01-01T00:00:00Z")
        with mock.patch.object(continuations, "_poll_job", return_value={
            "status": "ok", "job": {"status": "completed"},
        }):
            self.assertEqual(continuations.poll_due(self.root, self.sid)["status"], "ready")

    def claim(self):
        return continuations.claim_due(self.root, self.sid)

    def test_no_saved_goal_allows_explicit_readiness_continuation(self):
        self.assertEqual(self.schedule()["status"], "scheduled")
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_side_question_and_new_observation_do_not_cancel_saved_direction(self):
        self.capture("Inspect the upload flow", kind="direction")
        self.schedule()
        self.capture("What does this helper name mean?", kind="question")
        self.capture("Observed a second caller", kind="observation")
        # A newer human message alone says nothing about cancellation intent.
        import work_ledger
        work_ledger.mark_user_turn(self.root, self.sid)
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_new_explicit_direction_holds_old_resume_goal_without_claiming(self):
        self.capture("Inspect the upload flow", kind="direction")
        self.schedule()
        self.make_ready()
        self.capture("Stop the investigation; document setup only", kind="direction")
        result = self.claim()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["continuation"]["blocked_reason"], "direction_changed")
        self.assertEqual(result["continuation"]["attempts"], 0)
        self.assertEqual(result["continuation"]["last_job_status"], "completed")
        self.assertEqual(continuations.pending(self.root), [])

    def test_first_direction_after_no_goal_holds_old_automatic_action(self):
        self.schedule()
        self.make_ready()
        self.capture("Investigate a different issue now", kind="direction")
        self.assertEqual(self.claim()["continuation"]["blocked_reason"], "direction_changed")

    def test_correction_superseding_direction_holds_old_automatic_action(self):
        goal = self.capture("Inspect the upload flow", kind="direction")
        self.schedule()
        self.make_ready()
        self.capture("The old investigation has ended", kind="correction", supersedes=[goal["id"]])
        self.assertEqual(self.claim()["continuation"]["blocked_reason"], "direction_changed")

    def test_unrelated_source_correction_does_not_cancel_readiness(self):
        self.capture("Inspect the upload flow", kind="direction")
        finding = self.capture("Observed a caller", kind="observation")
        self.schedule()
        self.capture("Corrected the caller observation", kind="correction", supersedes=[finding["id"]])
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_private_direction_change_blocks_without_exposing_content_or_reference(self):
        self.schedule()
        self.make_ready()
        private = self.capture(
            "Private direction fixture do not copy", kind="direction",
            sensitivity="secret", index_policy="no_rag", allow_sensitive_plaintext=True,
        )
        result = self.claim()
        self.assertEqual(result["continuation"]["blocked_reason"], "direction_changed")
        state = project_workspace.session_state_path(self.root, self.sid).read_text(encoding="utf-8")
        # The session's existing last_capture_id is unrelated to the scheduler.
        record = json.loads(state)["continuation"]
        for representation in (json.dumps(result), json.dumps(record)):
            self.assertNotIn(private["summary"], representation)
            self.assertNotIn(private["id"], representation)

    def test_unreadable_or_missing_direction_state_is_not_empty_goal(self):
        self.schedule()
        self.make_ready()
        journal = project_workspace.paths_for(self.root, "demo").continuity
        journal.unlink()
        result = self.claim()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["continuation"]["blocked_reason"], "direction_state_unavailable")
        self.assertEqual(self.schedule()["reason"], "direction_state_unavailable")

    def test_corrupt_direction_state_blocks_auto_resume(self):
        self.schedule()
        self.make_ready()
        journal = project_workspace.paths_for(self.root, "demo").continuity
        with journal.open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "direction", BROKEN\n')
        self.assertEqual(self.claim()["continuation"]["blocked_reason"], "direction_state_unavailable")

    def test_legacy_continuation_needs_reconciliation_and_rescheduling(self):
        self.schedule()
        self.make_ready()
        self.update_record(schema_version=2, direction_fingerprint="")
        result = self.claim()
        self.assertEqual(result["continuation"]["blocked_reason"], "direction_binding_missing")
        self.assertEqual(self.schedule()["status"], "scheduled")
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_explicit_rescheduling_binds_new_direction_and_preserves_active_budget(self):
        self.schedule()
        self.make_ready()
        claimed = self.claim()["continuation"]
        self.capture("Inspect a revised scope", kind="direction")
        scheduled = self.schedule()["continuation"]
        self.assertEqual(scheduled["attempts"], claimed["attempts"])
        self.assertEqual(scheduled["deadline_at"], claimed["deadline_at"])
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_expired_claim_rechecks_saved_direction(self):
        self.schedule()
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")
        self.update_record(lease_until="2000-01-01T00:00:00Z")
        self.capture("Stop the old scope", kind="direction")
        self.assertEqual(self.claim()["continuation"]["blocked_reason"], "direction_changed")

    def test_paused_attached_session_is_held_but_explicit_unattached_session_works(self):
        self.schedule()
        self.make_ready()
        project_workspace.detach_session_project(self.root, "demo", session_id=self.sid)
        self.assertEqual(self.claim()["status"], "scope_conflict")
        self.sid = "deliberately-unattached"
        self.assertEqual(self.schedule()["status"], "scheduled")
        self.make_ready()
        self.assertEqual(self.claim()["status"], "due")

    def test_late_job_poll_does_not_resurrect_cancelled_or_finalized_continuation(self):
        for stop, expected in ((continuations.cancel, "cancelled"), (continuations.finalize, "done")):
            with self.subTest(status=expected):
                self.schedule()
                self.update_record(not_before="2000-01-01T00:00:00Z")

                def observe(*_):
                    stop(self.root, self.sid)
                    return {"status": "ok", "job": {"status": "completed"}}

                with mock.patch.object(continuations, "_poll_job", side_effect=observe):
                    result = continuations.poll_due(self.root, self.sid)
                self.assertEqual(result["status"], expected)
                self.assertEqual(continuations.status(self.root, self.sid)["continuation"]["status"], expected)

    def test_failed_prompt_release_does_not_reopen_cancelled_continuation(self):
        self.schedule()
        self.make_ready()
        claimed = self.claim()["continuation"]
        continuations.cancel(self.root, self.sid)
        result = continuations.release(self.root, self.sid, generation=claimed["generation"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(continuations.pending(self.root), [])


if __name__ == "__main__":
    unittest.main()
