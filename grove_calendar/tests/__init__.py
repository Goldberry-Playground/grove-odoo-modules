# Auto-discovered by Odoo's test runner when invoked with --test-enable.
#
# EVERY Odoo-native (TransactionCase) test module MUST be listed here or Odoo
# silently never runs it — a green install-smoke-test does NOT mean the suite ran
# (grove-odoo native-test-discovery gotcha; GOL-1936/GOL-1941).
#
# test_discord_format is pure stdlib (also run by the pytest job); it is listed
# here so the Odoo runner exercises it too.
from . import (
    test_digest_cron,  # noqa: F401
    test_discord_events_cron,  # noqa: F401
    test_discord_exceptions,  # noqa: F401
    test_discord_format,  # noqa: F401
    test_discord_recurrence,  # noqa: F401
    test_events_feed_route,  # noqa: F401
    test_grove_public_domain,  # noqa: F401
    test_grove_website_domain,  # noqa: F401
)
