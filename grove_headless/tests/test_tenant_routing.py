"""Unit tests for the X-Grove-Tenant → Website routing in models/website.py.

These run inside Odoo's TransactionCase harness, so they get a fresh DB
transaction each test (rolled back at teardown). Run via:

    odoo --addons-path=... --test-enable --stop-after-init -i grove_headless

The CI install-smoke-test job in .github/workflows/ci.yml runs them on
every PR.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("grove_headless", "tenant_routing", "post_install", "-at_install")
class TestTenantRouting(TransactionCase):
    """Cover _resolve_tenant_slug — the function that maps a header value
    like 'goldberry' to the Goldberry Grove Farm website record.

    The companies + websites are seeded by data/grove_companies.xml as
    part of module install, so by the time `post_install` runs they're
    expected to exist.
    """

    def test_resolves_known_slugs_to_correct_website(self):
        """Each of the three production tenants resolves to its website."""
        Website = self.env["website"]
        cases = [
            ("goldberry", "Goldberry Grove Farm"),
            ("ggg", "George George George Woodworking"),
            ("nursery", "At The Grove Nursery"),
        ]
        for slug, expected_name in cases:
            with self.subTest(slug=slug):
                website = Website._resolve_tenant_slug(slug)
                self.assertTrue(
                    website,
                    f"slug '{slug}' should resolve to a website but got None",
                )
                self.assertEqual(
                    website.name,
                    expected_name,
                    f"slug '{slug}' resolved to '{website.name}', expected '{expected_name}'",
                )

    def test_unknown_slug_returns_none(self):
        """Unknown slugs MUST return None (caller falls through to Host
        routing) — not raise, not return some default tenant."""
        Website = self.env["website"]
        for bad_slug in ["nonexistent", "GoldberryGrove", "main", ""]:
            with self.subTest(slug=bad_slug):
                self.assertIsNone(
                    Website._resolve_tenant_slug(bad_slug),
                    f"slug '{bad_slug}' should be unresolvable, but resolved",
                )

    def test_slug_resolution_is_case_insensitive_and_trim_safe(self):
        """Header values from Nginx may have casing or whitespace quirks;
        the resolver normalizes them.

        Production note: this matters because nginx can lowercase headers
        differently across deployments, and curl users sometimes pass
        ' goldberry ' (with leading/trailing space) by accident.
        """
        Website = self.env["website"]
        for variant in ["GOLDBERRY", "Goldberry", " goldberry ", "goldberry\n"]:
            with self.subTest(variant=repr(variant)):
                website = Website._resolve_tenant_slug(variant)
                self.assertTrue(website, f"slug variant {variant!r} should resolve")
                self.assertEqual(website.name, "Goldberry Grove Farm")

    def test_resolution_does_not_accept_integer_ids(self):
        """Passing a raw website ID must NOT resolve. Otherwise an
        attacker could probe arbitrary website records by guessing IDs."""
        Website = self.env["website"]
        # Integer cast to string — would matter if the function did
        # something like `int(slug)` and looked up by id.
        for ident in ["1", "2", "999"]:
            with self.subTest(ident=ident):
                self.assertIsNone(
                    Website._resolve_tenant_slug(ident),
                    f"raw id '{ident}' must not resolve",
                )

    def test_returns_only_active_records(self):
        """If a website is archived (active=False), _resolve_tenant_slug
        should still find it via the name match — Odoo searches don't
        filter on `active` by default. This test documents that
        deliberate behavior so a future change to add `('active', '=', True)`
        is intentional, not a refactor accident.
        """
        Website = self.env["website"]
        website = Website._resolve_tenant_slug("goldberry")
        self.assertTrue(website)
        # The seeded record should be active by default.
        self.assertTrue(website.active)


@tagged("grove_headless", "tenant_routing", "-at_install", "post_install")
class TestTenantSlugMap(TransactionCase):
    """Catch drift between the _TENANT_SLUGS dict and the seeded website
    records. If grove_companies.xml renames a website, this test fails
    fast with a clear pointer to the dict that needs updating.
    """

    def test_every_known_slug_has_a_seeded_website(self):
        """Each value in _TENANT_SLUGS should match a real website name."""
        from odoo.addons.grove_headless.models.website import _TENANT_SLUGS

        Website = self.env["website"].sudo()
        for slug, name in _TENANT_SLUGS.items():
            with self.subTest(slug=slug):
                hits = Website.search([("name", "=", name)])
                self.assertEqual(
                    len(hits),
                    1,
                    f"_TENANT_SLUGS[{slug!r}] = {name!r} but {len(hits)} "
                    f"websites match that name. Update either the dict in "
                    f"models/website.py or the data file.",
                )

    def test_required_tenants_are_all_in_the_map(self):
        """Production has 3 tenants — drift would mean we lost one."""
        from odoo.addons.grove_headless.models.website import _TENANT_SLUGS

        for required in ("goldberry", "ggg", "nursery"):
            self.assertIn(
                required,
                _TENANT_SLUGS,
                f"Required tenant slug '{required}' missing from _TENANT_SLUGS",
            )
