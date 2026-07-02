# conftest.py — scoped to grove_headless/tests/ to allow pytest to collect
# pure-Python tests inside the grove_headless Odoo addon without triggering
# the real grove_headless __init__.py, which imports `odoo` (not available
# outside the Odoo runtime). Placement in this directory prevents stubs from
# affecting test collection elsewhere in the repo.
#
# The test files in grove_headless/tests/ use importlib.util.spec_from_file_location
# to load only the pure-Python modules they need; they never import via the
# Odoo package system.
#
# Note: the module stubs are set up in the root conftest.py before pytest
# imports any packages, so this conftest file can focus on test-specific setup.
