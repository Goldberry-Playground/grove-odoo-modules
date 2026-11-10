# Pure-Python planner-learning helpers, free of Odoo imports so they can be
# unit-tested by file path in CI (see grove_headless/tests/conftest.py). The
# learning-loop models (grove.planner.*) and controllers/planner.py import the
# rule defaults and the request-body validators from here, so the seed data,
# the engine fallback and the public routes all agree on one source of truth
# (GOL-3549, planner winter beta spec section B).
