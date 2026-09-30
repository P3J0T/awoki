"""Navigation cannot bypass canonical privacy, retirement or unreadable state."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import continuity
import project_workspace
import reference_catalog
import work_ledger


class ContinuityReferencePrivacyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sid = "references-session"
        project_workspace.project_create(self.root, "demo", session_id=self.sid)
        self.pp = project_workspace.paths_for(self.root, "demo")

    def capture(self, summary, **kwargs):
        return project_workspace.project_capture(
            self.root, "demo", summary, refresh=False, sync_index=False, **kwargs,
        )

    def describe(self, ref):
        return reference_catalog.describe(self.root, "demo", ref, session_id=self.sid)

    def resolve(self, text):
        return reference_catalog.resolve(self.root, "demo", text, session_id=self.sid)

    def touch(self, note, label=None):
        return work_ledger.touch_reference(
            self.root, self.sid, project_id="demo", reference_id=note["id"],
            label=label or note["summary"], why_saved="Fixture navigation",
        )

    def context(self):
        return reference_catalog.compact_context(self.root, "demo", session_id=self.sid)

    def test_private_and_no_rag_notes_are_unavailable_on_every_navigation_path(self):
        for privacy in ({"sensitivity": "secret"}, {"index_policy": "no_rag"}):
            with self.subTest(privacy=privacy):
                note = self.capture("Private fixture zebra direction", kind="direction", **privacy)
                self.touch(note)
                described = self.describe(note["id"])
                self.assertEqual(described["status"], "unavailable")
                self.assertNotIn("label", described)
                self.assertEqual(self.resolve(note["id"])["resolved_reference_id"], "")
                self.assertEqual(self.resolve("zebra direction")["matches"], [])
                rejected = reference_catalog.annotate(self.root, "demo", note["id"], label="Private alias")
                self.assertEqual(rejected["status"], "unavailable")
                self.assertNotIn(note["id"], self.context())
                self.assertNotIn(note["summary"], self.context())

    def test_superseded_note_and_annotations_do_not_reappear(self):
        old = self.capture("Retired orchard investigation", kind="direction")
        reference_catalog.annotate(self.root, "demo", old["id"], label="Old orchard alias", aliases=["orchard investigation"])
        self.touch(old, "Old orchard alias")
        new = self.capture("Current harbor investigation", kind="direction", supersedes=[old["id"]])
        self.assertEqual(self.describe(old["id"])["status"], "unavailable")
        self.assertEqual(self.resolve(old["id"])["resolved_reference_id"], "")
        resolved = self.resolve("orchard investigation")
        self.assertNotIn(old["id"], {row["reference_id"] for row in resolved["matches"]})
        self.assertEqual(resolved["resolved_reference_id"], "")
        self.assertEqual(self.describe(new["id"])["status"], "ok")
        self.assertNotIn(old["id"], self.describe(new["id"])["linked_refs"])
        self.assertEqual(self.context(), "")
        # Old exact IDs must not silently resolve to the replacement identity.
        self.assertNotIn(new["id"], json.dumps(self.describe(old["id"])))

    def test_legacy_private_catalog_alias_is_not_a_backdoor(self):
        note = self.capture("Hidden cactus summary", sensitivity="secret")
        reference_catalog._write_catalog(self.root, "demo", {"entries": {
            note["id"]: {"reference_id": note["id"], "label": "Leaked cactus alias", "aliases": ["cactus lookup"]},
        }})
        self.assertEqual(self.resolve("cactus lookup")["matches"], [])
        self.assertNotIn("cactus", json.dumps(self.describe(note["id"])))

    def test_current_public_note_is_available_and_uses_current_navigation_text(self):
        note = self.capture("Current receipt ordering", kind="observation")
        reference_catalog.annotate(self.root, "demo", note["id"], label="Reviewed receipt ordering", aliases=["receipt sequence"])
        self.touch(note, "Stale transient label")
        self.assertEqual(self.describe(note["id"])["label"], "Reviewed receipt ordering")
        self.assertEqual(self.resolve("receipt sequence")["resolved_reference_id"], note["id"])
        context = self.context()
        self.assertIn(note["id"], context)
        self.assertIn("Reviewed receipt ordering", context)
        self.assertNotIn("Stale transient label", context)

    def test_closed_direction_is_not_resumed_but_completed_finding_remains_recallable(self):
        for state in ("done", "complete", "completed", "closed", "resolved", "superseded", "retired", "cancelled", "canceled"):
            with self.subTest(state=state):
                goal = self.capture(f"Closed fixture direction {state}", kind="direction", state=state)
                self.assertEqual(self.describe(goal["id"])["status"], "unavailable")
                self.assertEqual(self.resolve(goal["id"])["resolved_reference_id"], "")
                self.touch(goal)
                self.assertNotIn(goal["id"], self.context())
        for state in ("done", "complete", "completed"):
            with self.subTest(finding_state=state):
                finding = self.capture(f"Completed source observation {state}", kind="finding", state=state)
                self.assertEqual(self.describe(finding["id"])["status"], "ok")
                self.touch(finding)
                self.assertIn(finding["id"], self.context())

    def test_corrupt_journal_is_unknown_without_leaking_cached_labels(self):
        note = self.capture("Cached maple summary", kind="direction")
        reference_catalog.annotate(self.root, "demo", note["id"], label="Cached maple alias")
        self.touch(note)
        with self.pp.continuity.open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        self.assertEqual(self.describe(note["id"])["reason"], "continuity_state_unavailable")
        for query in (note["id"], "maple"):
            resolved = self.resolve(query)
            self.assertEqual(resolved["status"], "unknown")
            self.assertEqual(resolved["resolution"]["status"], "unknown")
            self.assertEqual(resolved["resolved_reference_id"], "")
            self.assertEqual(resolved["matches"], [])
        self.assertEqual(self.context(), "")

    def test_missing_journal_is_unknown_and_cannot_overwrite_annotation(self):
        note = self.capture("Prior useful label")
        reference_catalog.annotate(self.root, "demo", note["id"], label="Existing alias")
        catalog = reference_catalog._catalog_path(self.root, "demo")
        before = catalog.read_bytes()
        self.pp.continuity.unlink()
        result = reference_catalog.annotate(self.root, "demo", note["id"], label="Wrong replacement")
        self.assertEqual(result["reason"], "continuity_state_unavailable")
        self.assertEqual(catalog.read_bytes(), before)

    def test_foreign_canonical_scope_does_not_supply_a_reference(self):
        note = self.capture("Unscoped hidden label")
        rows = [json.loads(line) for line in self.pp.continuity.read_text().splitlines()]
        rows[-1]["project_id"] = "other"
        self.pp.continuity.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        self.assertEqual(self.describe(note["id"])["reason"], "continuity_state_unavailable")
        self.assertEqual(self.resolve("hidden label")["resolved_reference_id"], "")

    def test_annotation_rejects_private_or_retired_continuity_links(self):
        public = self.capture("Public active note")
        private = self.capture("Private linked note", sensitivity="secret")
        old = self.capture("Retired linked note")
        self.capture("Updated linked note", kind="correction", supersedes=[old["id"]])
        for ref in (private["id"], old["id"]):
            result = reference_catalog.annotate(self.root, "demo", public["id"], linked_refs=[" " + ref + " "])
            self.assertEqual(result["reason"], "linked_continuity_reference_unavailable")
        self.assertFalse(reference_catalog._catalog_path(self.root, "demo").exists())

    def test_existing_annotation_links_are_revalidated(self):
        public = self.capture("Navigation anchor")
        linked = self.capture("Initially active linked note")
        result = reference_catalog.annotate(self.root, "demo", public["id"], linked_refs=[linked["id"]])
        self.assertEqual(result["linked_refs"], [linked["id"]])
        self.capture("Replacement linked note", kind="correction", supersedes=[linked["id"]])
        self.assertEqual(self.describe(public["id"])["linked_refs"], [])

    def test_natural_language_ambiguity_still_requires_exact_identity(self):
        first = self.capture("Common orange topic, first branch")
        second = self.capture("Common orange topic, second branch")
        resolved = self.resolve("common orange topic")
        self.assertEqual(resolved["resolution"]["status"], "ambiguous")
        self.assertEqual(resolved["resolved_reference_id"], "")
        self.assertEqual({row["reference_id"] for row in resolved["matches"]}, {first["id"], second["id"]})

    def test_candidate_pass_reads_canonical_journal_once(self):
        # Include both annotated and unannotated rows: deduplication must not
        # turn a candidate scan into one whole-journal read per descriptor.
        rows = [self.capture(f"Fixture catalog subject {number}") for number in range(12)]
        for row in rows[:6]:
            reference_catalog.annotate(self.root, "demo", row["id"], label=row["summary"])
        with mock.patch.object(continuity, "_read_goal_records", wraps=continuity._read_goal_records) as read:
            result = self.resolve("fixture catalog subject")
        self.assertTrue(result["matches"])
        self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
