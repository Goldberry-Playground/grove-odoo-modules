# conftest.py — allows pytest to collect pure-Python tests inside the
# grove_headless Odoo addon without triggering the real grove_headless
# __init__.py, which imports `odoo` (not available outside the Odoo runtime).
#
# The test files in grove_headless/tests/ use importlib.util.spec_from_file_location
# to load only the pure-Python modules they need; they never import via the
# Odoo package system.
import os
import sys
import types

_ROOT = os.path.dirname(__file__)


def _stub_pkg(name: str, real_path: str | None = None) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__package__ = name
    m.__path__ = [real_path] if real_path else []
    m.__file__ = None
    return m


# Stub odoo and the submodules that grove_headless/__init__.py pulls in.
for _mod in (
    "odoo",
    "odoo.http",
    "odoo.fields",
    "odoo.models",
    "odoo.api",
    "odoo.exceptions",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

# Stub grove_headless as a package whose __path__ points to the real directory
# so that sub-package imports resolve to the real files — but we replace only
# the top-level __init__ so it never runs.
_gh_dir = os.path.join(_ROOT, "grove_headless")
sys.modules.setdefault("grove_headless", _stub_pkg("grove_headless", _gh_dir))

_tests_dir = os.path.join(_gh_dir, "tests")
sys.modules.setdefault("grove_headless.tests", _stub_pkg("grove_headless.tests", _tests_dir))
