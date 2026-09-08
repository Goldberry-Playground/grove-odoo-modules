# Regression guard for the 2026-09-08 auto-merge race (#194 -> #204, fixed in
# #209): the morning rate-check PR merged with a RED CI suite because
# (a) auto-approve's check_run entrypoint armed auto-merge with no CI condition,
# (b) auto-merge armed on a previous day survives rate-check's daily force-push,
# and (c) only three lint/validate contexts are actually REQUIRED while the
# GOL-1953 promotion is parked, so a red "Pure-Python unit tests" did not block.
#
# The CI job installs no YAML parser, so these assert on the workflow text.
# They pin the two in-workflow gates that closed the race; if either block is
# removed or reordered, a failure here is the early warning that untested
# shipping_rates.json regenerations can reach main again.
import os
import re
import unittest

_WORKFLOWS = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".github", "workflows")


def _read(name):
    with open(os.path.join(_WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


class TestAutoApproveFullCiGate(unittest.TestCase):
    """auto-approve.yml must require the full CI suite green before approving/arming."""

    def setUp(self):
        self.text = _read("auto-approve.yml")

    def test_polls_ci_workflow_run_on_head_sha(self):
        # The gate queries the Actions runs API for the head SHA and selects the
        # "CI" workflow run — not just the minimal REQUIRED contexts.
        self.assertIn("actions/runs?head_sha=$HEAD_SHA", self.text)
        self.assertIn('select(.name=="CI")', self.text)

    def test_refuses_to_approve_unless_ci_succeeded(self):
        m = re.search(r'if \[ "\$CI_STATE" != "success" \].*?exit 0', self.text, re.DOTALL)
        self.assertIsNotNone(m, "auto-approve.yml must exit without approving when CI is not green")

    def test_ci_gate_runs_before_the_approval(self):
        gate = self.text.index("actions/runs?head_sha=$HEAD_SHA")
        approve = self.text.index("--approve")
        self.assertLess(gate, approve, "the full-CI gate must precede `gh pr review --approve`")


class TestRateCheckDisarmsStaleAutoMerge(unittest.TestCase):
    """rate-check.yml must disarm auto-merge after its daily force-push.

    GitHub auto-merge survives head-branch pushes: an arming from a previous
    day would otherwise merge today's untested rates the moment the minimal
    required contexts go green.
    """

    def setUp(self):
        self.text = _read("rate-check.yml")

    def test_disable_auto_present_and_after_force_push(self):
        push = self.text.index("git push -f origin chore/rate-check")
        disarm = self.text.index("--disable-auto")
        self.assertLess(push, disarm, "auto-merge must be disarmed AFTER the force-push")

    def test_disarm_is_flag_only_never_branch_delete(self):
        # GOL-1658: deleting the head ref cancels a queued merge and CLOSES the
        # PR unmerged. Disarming must never grow a --delete-branch.
        self.assertNotIn("--delete-branch", self.text)


if __name__ == "__main__":
    unittest.main()
