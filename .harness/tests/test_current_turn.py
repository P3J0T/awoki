"""Structural completion is current-turn bound, never inferred from CLI exit."""
import tempfile
import unittest
from pathlib import Path

import agent_runtime


class CurrentTurnTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def user(self, mid):
        return agent_runtime.user_turn(self.root, "session", message_id=mid)

    def terminal(self, mid, parent="u1", **changes):
        return agent_runtime.terminal_turn(self.root, "session", **{
            "message_id": mid, "parent_message_id": parent, "finish_reason": "stop",
            "has_reasoning": False, "has_text": True, "has_tool": False, **changes})

    def test_new_turn_and_missing_idle_never_inherit_prior_success(self):
        self.user("u1")
        done = self.terminal("a1")
        self.assertTrue(done["current_turn_complete"])
        self.assertEqual(done["runtime_state"], "ok")
        self.user("u2")
        pending = agent_runtime.status(self.root, "session")
        self.assertFalse(pending["current_turn_complete"])
        self.assertEqual(pending["runtime_state"], "incomplete")
        self.assertEqual(pending["last_terminal_turn"]["message_id"], "a1")
        # Late old-parent events and summary completion cannot finish u2.
        self.assertFalse(self.terminal("a-late")["current_turn_complete"])
        self.assertFalse(self.terminal("summary", "u2", is_summary=True)["current_turn_complete"])
        self.assertTrue(self.terminal("a2", "u2")["current_turn_complete"])
        self.assertTrue(self.user("u2")["current_turn_complete"], "duplicate user update must not invalidate completion")

    def test_tool_only_denied_empty_interrupted_and_limit_are_incomplete(self):
        for changes in ({"finish_reason": "tool-calls", "has_tool": True},
                        {"finish_reason": "tool-calls", "has_tool": True, "has_text": False},
                        {"has_text": False}, {"error_type": "AbortError"},
                        {"finish_reason": "length"}, {"finish_reason": ""}):
            with self.subTest(changes=changes):
                self.user(str(changes))
                result = self.terminal(str(changes), str(changes), **changes)
                self.assertFalse(result["current_turn_complete"])
                self.assertTrue(result["unresolved_anomaly"])

    def test_missing_parent_and_legacy_state_cannot_certify_current_turn(self):
        self.assertFalse(agent_runtime.status(self.root, "session")["current_turn_complete"])
        self.user("u1")
        self.assertFalse(self.terminal("legacy", "")["current_turn_complete"])


if __name__ == "__main__":
    unittest.main()
