# Auto-discovered by Odoo's test runner when invoked with --test-enable.
#
# EVERY Odoo-native (TransactionCase/HttpCase) test module MUST be listed here
# or Odoo silently never runs it — green CI does not mean the suite ran (see the
# grove-odoo native-test-discovery gotcha). A module that also appears in the
# root conftest's `collect_ignore_glob` is skipped by pytest too; missing from
# BOTH lists is the "double-skip" that leaves a suite fully dormant (GOL-1936).
from . import (
    test_availability_events,  # noqa: F401
    test_detail_serialization,  # noqa: F401
    test_effective_shipping_tier,  # noqa: F401
    test_growing_facts,  # noqa: F401
    test_kit_boms,  # noqa: F401
    test_newsletter_subscribe,  # noqa: F401
    test_pos,  # noqa: F401
    test_potting_batch,  # noqa: F401
    test_preorder_label_skip,  # noqa: F401
    test_product_slug,  # noqa: F401
    test_publish_event,  # noqa: F401
    test_shipping_calendar,  # noqa: F401
    test_shipping_rates_feed,  # noqa: F401
    test_shipping_zones,  # noqa: F401
    test_shippo_client,  # noqa: F401
    test_stripe_checkout,  # noqa: F401
    test_tenant_routing,  # noqa: F401
    test_wv_taxes,  # noqa: F401
)
