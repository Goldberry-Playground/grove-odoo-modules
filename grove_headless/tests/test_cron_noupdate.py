"""GOL-3562: every shipped ir.cron must load under noupdate="1".

Pure-Python (no DB, no ``odoo`` import): parse the shipped cron XML directly so
the check runs under plain pytest. Mirrors ``test_seed_filters.py``.

Background: ``order_rollup_cron.xml`` and ``listing_audit_cron.xml`` pinned a
fixed past ``nextcall`` while sitting under a bare ``<odoo>`` (the default
``noupdate="0"``). Every ``-u grove_headless`` therefore rewrote the live
``ir.cron`` row — including ``nextcall`` — back to the past, and the scheduler
fired the job on its next tick (the weekly digest fired at 10:09 UTC on
2026-10-21, right after a 10:08 module update, instead of Monday 08:00 UTC).

The fix, and the house convention for every cron in this directory, is to load
``ir.cron`` records under ``noupdate="1"`` so an upgrade never resets a live
schedule. This test fails if any ``*_cron.xml`` record regresses to an
updatable container.
"""

import glob
import os
import unittest
from xml.etree import ElementTree as ET

_HERE = os.path.dirname(__file__)
_DATA_DIR = os.path.join(_HERE, "..", "data")


def _unprotected_cron_ids(path):
    """Return the xmlids of ir.cron records in ``path`` NOT under noupdate="1".

    A record is protected if either the root ``<odoo>`` element or its wrapping
    ``<data>`` element carries ``noupdate="1"`` — the two forms Odoo's data
    loader honours.
    """
    root = ET.parse(path).getroot()
    root_noupdate = root.get("noupdate") == "1"
    unprotected = []
    for child in root:
        if child.tag == "record":
            if child.get("model") == "ir.cron" and not root_noupdate:
                unprotected.append(child.get("id"))
        elif child.tag == "data":
            data_noupdate = child.get("noupdate") == "1"
            for rec in child.findall("record"):
                if rec.get("model") == "ir.cron" and not (root_noupdate or data_noupdate):
                    unprotected.append(rec.get("id"))
    return unprotected


class TestCronNoupdate(unittest.TestCase):
    def setUp(self):
        self.cron_files = sorted(glob.glob(os.path.join(_DATA_DIR, "*_cron.xml")))

    def test_cron_files_discovered(self):
        # Guard against the glob silently matching nothing (moved dir, rename).
        self.assertTrue(self.cron_files, "no *_cron.xml files found under data/")

    def test_every_cron_record_is_noupdate(self):
        offenders = {}
        for path in self.cron_files:
            bad = _unprotected_cron_ids(path)
            if bad:
                offenders[os.path.basename(path)] = bad
        self.assertEqual(
            offenders,
            {},
            "ir.cron records must load under noupdate=\"1\" so `-u` never resets "
            "a live schedule (GOL-3562); offenders: %s" % offenders,
        )


if __name__ == "__main__":
    unittest.main()
