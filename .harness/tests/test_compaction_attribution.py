"""Structural native continuation metadata preserves the true parent and user boundary."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import agent_runtime
import opencode_events


class CompactionAttributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        agent_runtime.user_turn(self.root, "session", message_id="human")

    def terminal(self, **changes):
        return opencode_events.record_agent_terminal_turn(self.root, "session", **{
            "message_id": "answer", "parent_message_id": "synthetic-user",
            "finish_reason": "stop", "has_reasoning": False, "has_text": True, "has_tool": False,
            "compaction_continuation_of": "human", "compaction_summary_message_id": "summary",
            "compaction_marker_message_id": "marker", **changes,
        })

    def test_verified_continuation_completes_original_user_without_reparenting(self):
        result = self.terminal()
        self.assertTrue(result["current_turn_complete"])
        self.assertEqual(result["current_turn"]["user_message_id"], "human")
        terminal = result["last_terminal_turn"]
        self.assertEqual(terminal["parent_message_id"], "synthetic-user")
        self.assertEqual(terminal["compaction_continuation"], {
            "user_message_id": "human", "summary_message_id": "summary", "marker_message_id": "marker"})
        self.assertEqual(self.terminal()["status"], "duplicate")
        self.assertTrue(agent_runtime.status(self.root, "session")["current_turn_complete"])

    def test_summary_and_nonanswers_never_complete_original_user(self):
        for changes in ({"is_summary": True}, {"finish_reason": "length"}, {"has_text": False},
                        {"finish_reason": "tool-calls", "has_tool": True}, {"error_type": "AbortError"}):
            with self.subTest(changes=changes):
                result = self.terminal(message_id="terminal-" + str(changes), **changes)
                self.assertFalse(result["current_turn_complete"])
        self.assertTrue(agent_runtime.status(self.root, "session")["last_compaction_turn"]["is_summary"])

    def test_new_user_invalidates_inflight_attribution_in_runtime_too(self):
        agent_runtime.user_turn(self.root, "session", message_id="new-human")
        result = self.terminal()
        self.assertFalse(result["current_turn_complete"])
        self.assertEqual(result["current_turn"]["user_message_id"], "new-human")

    def test_conflicting_explicit_attribution_cannot_fall_back_to_native_parent(self):
        agent_runtime.user_turn(self.root, "session", message_id="synthetic-user")
        result = self.terminal()
        self.assertFalse(result["current_turn_complete"])
        self.assertEqual(result["current_turn"]["user_message_id"], "synthetic-user")

    def test_missing_partial_or_inconsistent_attribution_remains_incomplete(self):
        for index, changes in enumerate((
            {"compaction_continuation_of": ""}, {"compaction_summary_message_id": ""},
            {"compaction_marker_message_id": ""}, {"compaction_continuation_of": "other-human"},
            {"parent_message_id": ""}, {"parent_message_id": "marker"},
            {"message_id": "summary"},
        )):
            with self.subTest(changes=changes):
                result = self.terminal(**{"message_id": "answer-" + str(index), **changes})
                self.assertFalse(result["current_turn_complete"])

    def test_bridge_cli_accepts_only_structural_attribution_and_keeps_native_parent(self):
        output = io.StringIO()
        with patch.object(opencode_events.HarnessPaths, "from_env", return_value=SimpleNamespace(root=self.root)), contextlib.redirect_stdout(output):
            code = opencode_events.main([
                "agent-turn-terminal", "--session-id", "session", "--message-id", "answer",
                "--parent-message-id", "synthetic-user", "--finish-reason", "stop", "--has-text",
                "--compaction-continuation-of", "human", "--compaction-summary-message-id", "summary",
                "--compaction-marker-message-id", "marker",
            ])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["current_turn_complete"])
        self.assertEqual(result["last_terminal_turn"]["parent_message_id"], "synthetic-user")
        self.assertNotIn("text", result["last_terminal_turn"])
        self.assertNotIn("reasoning", result["last_terminal_turn"])

    def test_ordinary_direct_parent_behavior_is_unchanged(self):
        result = self.terminal(parent_message_id="human", compaction_continuation_of="",
                               compaction_summary_message_id="", compaction_marker_message_id="")
        self.assertTrue(result["current_turn_complete"])
        self.assertNotIn("compaction_continuation", result["last_terminal_turn"])


if __name__ == "__main__":
    unittest.main()
