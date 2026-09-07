# Auto-discovered by Odoo's test runner when invoked with --test-enable.
#
# EVERY Odoo-native (TransactionCase/HttpCase) test module MUST be listed here
# or Odoo silently never runs it — a green CI check does NOT mean the suite ran
# (grove-odoo native-test-discovery gotcha; GOL-1936/GOL-1941).
from . import test_grove_support  # noqa: F401
