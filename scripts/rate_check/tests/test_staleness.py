"""Freshness guard tests (GOL-2641).

The point of this guard is that it must go green -> red ONCE on a dated
deadline, independently of whether the quote source is reachable. So the whole
verdict matrix is pinned here with an injected `today`, and the shipped table is
asserted to actually carry the provenance stamp the guard depends on.
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date


def _load(name, filename):
    path = os.path.join(os.path.dirname(__file__), "..", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


st = _load("staleness", "staleness.py")
rc = _load("rate_check_for_staleness", "rate_check.py")

VERIFIED = date(2026, 9, 21)


def _doc(stamp="2026-09-21", **extra):
    doc = {"_comment": "x", "_schema": 3, "zone_1": {"small": {"base": 18.0}}}
    if stamp is not None:
        doc[st.STAMP_KEY] = stamp
    doc.update(extra)
    return doc


class TestVerifiedOn(unittest.TestCase):
    def test_parses_the_stamp(self):
        self.assertEqual(st.verified_on(_doc()), VERIFIED)

    def test_absent_stamp_is_none(self):
        self.assertIsNone(st.verified_on(_doc(stamp=None)))

    def test_unparsable_stamp_is_none_not_an_exception(self):
        # A typo must fall through to the `unstamped` fail-safe, never crash the
        # guard (a crash in CI reads as "broken check", not "rates unproven").
        for bad in ("2026-13-99", "yesterday", "", "  ", "09/21/2026"):
            self.assertIsNone(st.verified_on(_doc(stamp=bad)), bad)

    def test_non_string_stamp_is_none(self):
        for bad in (20260921, None, {"date": "2026-09-21"}, ["2026-09-21"]):
            self.assertIsNone(st.verified_on({st.STAMP_KEY: bad}))

    def test_whitespace_is_tolerated(self):
        self.assertEqual(st.verified_on(_doc(stamp=" 2026-09-21 ")), VERIFIED)


class TestAgeDays(unittest.TestCase):
    def test_age_is_days_since_the_stamp(self):
        self.assertEqual(st.age_days(_doc(), date(2026, 9, 29)), 8)

    def test_same_day_is_zero(self):
        self.assertEqual(st.age_days(_doc(), VERIFIED), 0)

    def test_future_stamp_clamps_to_zero(self):
        # Clock skew / a fat-fingered date must not read as "extra fresh".
        self.assertEqual(st.age_days(_doc(), date(2026, 9, 1)), 0)

    def test_unstamped_age_is_none(self):
        self.assertIsNone(st.age_days(_doc(stamp=None), VERIFIED))


class TestAssessVerdicts(unittest.TestCase):
    def _verdict(self, today, **kw):
        return st.assess(_doc(), today, **kw)["verdict"]

    def test_fresh_below_the_warn_threshold(self):
        self.assertEqual(self._verdict(VERIFIED), "fresh")
        self.assertEqual(self._verdict(date(2026, 10, 4)), "fresh")  # 13 days

    def test_aging_at_the_warn_boundary(self):
        self.assertEqual(self._verdict(date(2026, 10, 5)), "aging")  # 14 days
        self.assertEqual(self._verdict(date(2026, 10, 18)), "aging")  # 27 days

    def test_stale_at_the_fail_boundary(self):
        self.assertEqual(self._verdict(date(2026, 10, 19)), "stale")  # 28 days

    def test_aging_still_exits_zero_but_warns(self):
        r = st.assess(_doc(), date(2026, 10, 6))
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("hard limit", r["message"])

    def test_stale_exits_one_and_names_the_date_and_age(self):
        r = st.assess(_doc(), date(2026, 10, 25))
        self.assertEqual(r["exit_code"], 1)
        self.assertIn("2026-09-21", r["message"])
        self.assertIn("34 days ago", r["message"])

    def test_thresholds_are_overridable(self):
        self.assertEqual(self._verdict(date(2026, 9, 23), warn_days=1, fail_days=2), "stale")

    def test_unstamped_real_table_fails_closed(self):
        r = st.assess(_doc(stamp=None), VERIFIED)
        self.assertEqual(r["verdict"], "unstamped")
        self.assertEqual(r["exit_code"], 1)

    def test_provisional_table_cannot_be_stale(self):
        r = st.assess(_doc(stamp=None, _provisional=True), date(2027, 1, 1))
        self.assertEqual(r["verdict"], "provisional")
        self.assertEqual(r["exit_code"], 0)

    def test_empty_table_has_nothing_to_keep_fresh(self):
        r = st.assess({"_comment": "seed", "_schema": 3}, date(2027, 1, 1))
        self.assertEqual(r["verdict"], "provisional")
        self.assertEqual(r["exit_code"], 0)

    def test_provisional_wins_even_with_an_ancient_stamp(self):
        r = st.assess(_doc(stamp="2020-01-01", _provisional=True), date(2027, 1, 1))
        self.assertEqual(r["verdict"], "provisional")
        self.assertEqual(r["exit_code"], 0)


class TestHasPublishedRates(unittest.TestCase):
    def test_underscore_keys_alone_are_not_published_rates(self):
        self.assertFalse(st.has_published_rates({"_comment": "x", "_schema": 3}))

    def test_an_empty_zone_is_not_published_rates(self):
        self.assertFalse(st.has_published_rates({"zone_1": {}}))

    def test_a_populated_zone_is(self):
        self.assertTrue(st.has_published_rates({"zone_1": {"small": {"base": 1.0}}}))


class TestCli(unittest.TestCase):
    def _run(self, doc, argv):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
            path = fh.name
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = st.main(["--rates-file", path] + argv)
            return code, out.getvalue(), err.getvalue()
        finally:
            os.unlink(path)

    def test_fresh_exits_zero_with_no_annotation(self):
        code, out, err = self._run(_doc(), ["--today", "2026-09-22"])
        self.assertEqual(code, 0)
        self.assertIn("[fresh]", out)
        self.assertNotIn("::", out)

    def test_aging_exits_zero_with_a_warning_annotation(self):
        code, out, _ = self._run(_doc(), ["--today", "2026-10-06"])
        self.assertEqual(code, 0)
        self.assertIn("::warning::", out)

    def test_stale_exits_one_with_an_error_annotation_on_stderr(self):
        code, _, err = self._run(_doc(), ["--today", "2026-10-30"])
        self.assertEqual(code, 1)
        self.assertIn("::error::", err)
        self.assertIn("[stale]", err)

    def test_unstamped_exits_one(self):
        code, _, err = self._run(_doc(stamp=None), ["--today", "2026-09-22"])
        self.assertEqual(code, 1)
        self.assertIn("[unstamped]", err)

    def test_max_age_days_override(self):
        code, _, _ = self._run(_doc(), ["--today", "2026-09-25", "--max-age-days", "3"])
        self.assertEqual(code, 1)


class TestShippedTableIsStamped(unittest.TestCase):
    """The guard is useless if the table it guards has no stamp — pin that."""

    def test_shipped_table_carries_a_parsable_stamp(self):
        with open(st.RATES_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIsNotNone(
            st.verified_on(doc),
            f"grove_headless/data/shipping_rates.json must carry a parsable `{st.STAMP_KEY}`",
        )

    def test_staleness_and_rate_check_agree_on_the_rates_path(self):
        # Two modules resolve the money-path table independently; if they ever
        # drift apart the guard silently checks the wrong file.
        self.assertEqual(os.path.realpath(st.RATES_PATH), os.path.realpath(rc.RATES_PATH))

    def test_stamp_is_invisible_to_the_odoo_loader(self):
        # shipping_zones._load_rates drops every underscore key, so provenance
        # can never reach a published rate. Pin the prefix contract.
        self.assertTrue(st.STAMP_KEY.startswith("_"))


if __name__ == "__main__":
    unittest.main()
