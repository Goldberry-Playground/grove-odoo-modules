# Auto-discovered by Odoo's test runner when invoked with --test-enable.
#
# EVERY Odoo-native (TransactionCase/HttpCase) test module MUST be listed here
# or Odoo silently never runs it — a green CI check does NOT mean the suite ran
# (grove-odoo native-test-discovery gotcha). Each file here is also listed in the
# root conftest.py collect_ignore_glob so pytest does not import them. The pure
# test (test_hr_pure.py) is intentionally NOT listed here: it is a pytest file.
from . import (
    test_aspect_coverage,  # noqa: F401
    test_projects,  # noqa: F401
)
