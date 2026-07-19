# conftest.py (root) — Sets up module stubs to prevent pytest from importing
# the real grove_headless/__init__.py during collection. The main conftest is in
# grove_headless/tests/conftest.py, scoped to that directory so stubs don't
# affect test collection elsewhere in the repo.
import os
import sys
import types


def _stub_pkg(name: str, real_path: str | None = None) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__package__ = name
    m.__path__ = [real_path] if real_path else []
    m.__file__ = None
    return m


# Stub odoo and the submodules that grove_headless/__init__.py pulls in.
for _mod in ("odoo", "odoo.http", "odoo.fields", "odoo.models", "odoo.api", "odoo.exceptions"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# Stub grove_headless as a package so pytest doesn't import the real __init__.py.
_ROOT = os.path.dirname(__file__)
_gh_dir = os.path.join(_ROOT, "grove_headless")
sys.modules.setdefault("grove_headless", _stub_pkg("grove_headless", _gh_dir))
sys.modules.setdefault(
    "grove_headless.controllers", _stub_pkg("grove_headless.controllers", os.path.join(_gh_dir, "controllers"))
)
sys.modules.setdefault("grove_headless.models", _stub_pkg("grove_headless.models", os.path.join(_gh_dir, "models")))
sys.modules.setdefault("grove_headless.tests", _stub_pkg("grove_headless.tests", os.path.join(_gh_dir, "tests")))

# The files below need a real Odoo runtime (TransactionCase etc.). They run
# under Odoo's --test-enable runner, not pytest — exclude them from pytest
# collection entirely so the odoo stubs above can't give them a confusing
# half-imported death. Pure-Python tests (loaded via importlib by file path)
# stay collected.
collect_ignore_glob = [
    "grove_headless/tests/test_growing_facts.py",
    "grove_headless/tests/test_effective_shipping_tier.py",
    "grove_headless/tests/test_detail_serialization.py",
    "grove_headless/tests/test_wv_taxes.py",
    "grove_headless/tests/test_tenant_routing.py",
    "grove_headless/tests/test_kit_boms.py",
    "grove_headless/tests/test_potting_batch.py",
    "grove_headless/tests/test_product_slug.py",
    "grove_headless/tests/test_pos.py",
    "grove_headless/tests/test_newsletter_subscribe.py",
]
