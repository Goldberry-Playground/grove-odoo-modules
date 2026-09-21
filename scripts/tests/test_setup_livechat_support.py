"""Pure tests for the GOL-2021 livechat config core (no Odoo, no network).

Covers the two things most likely to silently ship wrong:
  * the chatbot capture shape (email collected, question captured, never a
    lead-creating step — grove_support owns leads); and
  * the Odoo-19 res.users group write (field `group_ids`, portal dropped in the
    same write that adds internal).
"""

import importlib.util
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "setup_livechat_support.py")
_spec = importlib.util.spec_from_file_location("setup_livechat_support", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestChatbotScript(unittest.TestCase):
    def test_steps_are_ordered_and_start_with_a_greeting(self):
        steps = mod.build_chatbot_steps()
        seqs = [s["sequence"] for s in steps]
        self.assertEqual(seqs, sorted(seqs), "steps must be in ascending sequence")
        self.assertEqual(len(set(seqs)), len(seqs), "sequences must be unique")
        self.assertEqual(steps[0]["step_type"], "text", "bot must greet first")

    def test_captures_question_and_email(self):
        types = [s["step_type"] for s in mod.build_chatbot_steps()]
        self.assertIn("question_email", types)
        self.assertTrue(any(t in ("free_input_multi", "free_input_single") for t in types))

    def test_hands_off_with_forward_operator_not_a_lead_step(self):
        types = [s["step_type"] for s in mod.build_chatbot_steps()]
        self.assertIn("forward_operator", types)
        self.assertFalse(mod.LEAD_CREATING_STEP_TYPES.intersection(types))

    def test_every_step_has_a_message(self):
        for s in mod.build_chatbot_steps():
            self.assertTrue(s.get("message", "").strip(), f"empty message in step {s}")

    def test_assert_capture_only_accepts_the_real_script(self):
        mod.assert_capture_only(mod.build_chatbot_steps())  # must not raise

    def test_assert_capture_only_rejects_a_lead_creating_script(self):
        bad = [
            {"sequence": 1, "step_type": "free_input_multi", "message": "q"},
            {"sequence": 2, "step_type": "question_email", "message": "e"},
            {"sequence": 3, "step_type": "create_lead_and_forward", "message": "x"},
        ]
        with self.assertRaises(ValueError):
            mod.assert_capture_only(bad)

    def test_assert_capture_only_rejects_a_script_without_email(self):
        bad = [
            {"sequence": 1, "step_type": "free_input_multi", "message": "q"},
            {"sequence": 2, "step_type": "forward_operator", "message": "x"},
        ]
        with self.assertRaises(ValueError):
            mod.assert_capture_only(bad)


class TestWesUserWrite(unittest.TestCase):
    USER_G = 11  # base.group_user (stand-in ids)
    PORTAL_G = 22  # base.group_portal

    def test_uses_group_ids_not_groups_id(self):
        cmds = mod.wes_user_write_commands(self.USER_G, self.PORTAL_G, has_portal=False)
        self.assertIn("group_ids", cmds)
        self.assertNotIn("groups_id", cmds)

    def test_new_internal_user_only_adds_group_user(self):
        cmds = mod.wes_user_write_commands(self.USER_G, self.PORTAL_G, has_portal=False)
        self.assertEqual(cmds["group_ids"], [(4, self.USER_G)])

    def test_portal_dropped_in_same_write_as_internal_added(self):
        cmds = mod.wes_user_write_commands(self.USER_G, self.PORTAL_G, has_portal=True)
        # Trap #2: the (3, portal) drop and (4, user) add must be in ONE command
        # list so the disjoint-groups constraint never sees a portal+internal user.
        self.assertIn((3, self.PORTAL_G), cmds["group_ids"])
        self.assertIn((4, self.USER_G), cmds["group_ids"])


class TestChannelRule(unittest.TestCase):
    def test_rule_is_always_visible_with_chatbot_attached(self):
        vals = mod.channel_rule_vals(channel_id=7, chatbot_script_id=99)
        self.assertEqual(vals["action"], "display_button")  # never hide-when-offline
        self.assertEqual(vals["chatbot_script_id"], 99)  # bot drives it 24/7
        self.assertEqual(vals["channel_id"], 7)
        self.assertEqual(vals["regex_url"], "/")  # matches every page


if __name__ == "__main__":
    unittest.main()
