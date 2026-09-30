"""Odoo-runtime tests for the shop-departments feature (GOL-2744).

Covers the migration/restructure of the public-category tree (IDs kept,
grove_slug backfilled to today's slugified names), the /catalog/nav shape, the
department filter's category-id resolution, slug stability across a rename, and
the notify-me waitlist department-name resolver.

Run via:
    odoo --addons-path=... --test-enable --stop-after-init -i grove_headless
"""

from odoo.addons.grove_headless.hooks import restructure_department_tree
from odoo.tests.common import TransactionCase, tagged

from ..controllers.main import (
    _catalog_nav,
    _dept_category_ids,
    _product_department,
    _resolve_waitlist_dept_names,
)
from ..controllers.product_domain import slugify
from ..models.newsletter import newsletter_tag_names
from .common import GroveTaxFixtureMixin


@tagged("grove_headless", "shop_departments", "post_install", "-at_install")
class TestDepartmentRestructure(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.Category = self.env["product.public.category"]
        # Stand up a "today"-shaped public-category tree: five orchard top-level
        # categories, the Food Forest Packages collection-to-be, and the existing
        # Mycoforestry category. Clear grove_slug afterwards to mimic the
        # pre-migration prod state (the column is new, so existing rows are NULL).
        self.today_names = [
            "Fruit Trees",
            "Nut Trees",
            "Berry & Nut Shrubs",
            "Fruiting Vines",
            "Native",
            "Food Forest Packages",
            "Mycoforestry",
        ]
        self.original = {}
        for name in self.today_names:
            cat = self.Category.create({"name": name})
            cat.grove_slug = False  # simulate a legacy row with no slug yet
            self.original[name] = cat.id

    def _by_name(self, name):
        return self.Category.search([("name", "=", name)], limit=1)

    def _by_slug(self, slug):
        return self.Category.search([("grove_slug", "=", slug)], limit=1)

    # ── Migration / restructure ─────────────────────────────────────────

    def test_existing_ids_are_kept(self):
        restructure_department_tree(self.env)
        # Every original category record still exists under its original id.
        self.assertTrue(self.Category.browse(self.original["Fruit Trees"]).exists())
        self.assertEqual(self.Category.browse(self.original["Mycoforestry"]).exists().id, self.original["Mycoforestry"])
        # Food Forest Packages was converted in place, not recreated.
        guilds = self._by_slug("guilds")
        self.assertEqual(guilds.id, self.original["Food Forest Packages"])

    def test_slug_backfilled_to_todays_slugified_name(self):
        restructure_department_tree(self.env)
        for name in ("Fruit Trees", "Nut Trees", "Berry & Nut Shrubs", "Fruiting Vines", "Native"):
            cat = self.Category.browse(self.original[name])
            self.assertEqual(cat.grove_slug, slugify(name), f"{name} slug should be its slugified name")

    def test_food_forest_packages_becomes_guilds_collection(self):
        restructure_department_tree(self.env)
        guilds = self._by_slug("guilds")
        self.assertEqual(guilds.name, "Guilds")
        self.assertEqual(guilds.grove_node_kind, "collection")
        self.assertFalse(guilds.parent_id)

    def test_mycoforestry_becomes_coming_soon_department(self):
        restructure_department_tree(self.env)
        myco = self.Category.browse(self.original["Mycoforestry"])
        self.assertEqual(myco.grove_slug, "mycoforestry")
        self.assertEqual(myco.grove_node_kind, "department")
        self.assertEqual(myco.grove_dept_status, "coming_soon")
        child_names = set(myco.child_id.mapped("name"))
        self.assertEqual(child_names, {"Truffle trees", "Porcini trees"})

    def test_orchard_department_adopts_existing_categories(self):
        restructure_department_tree(self.env)
        orchard = self._by_slug("orchard-food-forest")
        self.assertEqual(orchard.grove_node_kind, "department")
        self.assertEqual(orchard.grove_dept_status, "live")
        adopted = set(orchard.child_id.mapped("name"))
        self.assertLessEqual(
            {"Fruit Trees", "Nut Trees", "Berry & Nut Shrubs", "Fruiting Vines", "Native"},
            adopted,
        )
        # An adopted category is now a `category` node, not a department.
        self.assertEqual(self.Category.browse(self.original["Fruit Trees"]).grove_node_kind, "category")

    def test_new_coming_soon_departments_created(self):
        restructure_department_tree(self.env)
        ff = self._by_slug("forest-farming")
        ss = self._by_slug("seed-and-scion")
        self.assertEqual(ff.grove_dept_status, "coming_soon")
        self.assertEqual(ss.grove_dept_status, "coming_soon")
        self.assertEqual(set(ff.child_id.mapped("name")), {"Medicinal roots", "Woodland edibles"})
        self.assertEqual(set(ss.child_id.mapped("name")), {"Scion wood", "Seed", "Rootstock"})

    def test_restructure_is_idempotent(self):
        restructure_department_tree(self.env)
        depts_first = self.Category.search_count([("grove_node_kind", "=", "department")])
        restructure_department_tree(self.env)
        depts_second = self.Category.search_count([("grove_node_kind", "=", "department")])
        self.assertEqual(depts_first, depts_second)
        # Still exactly one Guilds collection.
        self.assertEqual(self.Category.search_count([("grove_slug", "=", "guilds")]), 1)

    # ── Slug stability ──────────────────────────────────────────────────

    def test_slug_stable_across_category_rename(self):
        restructure_department_tree(self.env)
        cat = self.Category.browse(self.original["Fruit Trees"])
        self.assertEqual(cat.grove_effective_slug(), "fruit-trees")
        cat.name = "Fruit & Orchard Trees"  # a rename that would change slugify(name)
        self.assertEqual(cat.grove_effective_slug(), "fruit-trees", "authored slug must survive a rename")

    def test_effective_slug_falls_back_to_slugified_name(self):
        loose = self.Category.create({"name": "Loose Category"})
        loose.grove_slug = False
        self.assertEqual(loose.grove_effective_slug(), "loose-category")

    # ── dept= filter resolution ─────────────────────────────────────────

    def test_dept_category_ids_returns_root_plus_descendants(self):
        restructure_department_tree(self.env)
        ids = _dept_category_ids(self.env, "mycoforestry")
        myco = self.Category.browse(self.original["Mycoforestry"])
        self.assertIn(myco.id, ids)
        for child in myco.child_id:
            self.assertIn(child.id, ids)

    def test_dept_category_ids_unknown_slug_is_empty(self):
        restructure_department_tree(self.env)
        self.assertEqual(_dept_category_ids(self.env, "does-not-exist"), [])

    # ── /catalog/nav shape ──────────────────────────────────────────────

    def test_catalog_nav_shape_and_visibility(self):
        restructure_department_tree(self.env)
        company = self.env.company
        # Publish one product under Fruit Trees so Orchard (live) has >=1 product.
        self.env["product.template"].create(
            {
                "name": "Honeycrisp Apple",
                "website_published": True,
                "public_categ_ids": [(6, 0, [self.original["Fruit Trees"]])],
            }
        )
        nav = _catalog_nav(self.env, company)
        self.assertIn("departments", nav)
        self.assertIn("guilds", nav)

        by_slug = {d["slug"]: d for d in nav["departments"]}
        # Orchard (live, has a product) is shown with its category rows + count.
        self.assertIn("orchard-food-forest", by_slug)
        orchard = by_slug["orchard-food-forest"]
        self.assertEqual(orchard["status"], "live")
        self.assertEqual(orchard["kind"], "department")
        self.assertEqual(orchard["product_count"], 1)
        fruit = next(c for c in orchard["categories"] if c["slug"] == "fruit-trees")
        self.assertEqual(fruit["count"], 1)
        # Coming-soon departments are always shown, with facets from the allowlist.
        self.assertIn("mycoforestry", by_slug)
        self.assertTrue(set(by_slug["mycoforestry"]["facets"]).issubset({"host_tree", "fungus", "zone", "ships"}))
        # Guilds collection surfaces as its own node.
        self.assertEqual(nav["guilds"]["slug"], "guilds")
        self.assertEqual(nav["guilds"]["kind"], "collection")

    def test_catalog_nav_hides_hidden_and_empty_live_departments(self):
        restructure_department_tree(self.env)
        # A live department with zero published products must not render.
        ff = self._by_slug("forest-farming")
        ff.grove_dept_status = "live"
        # A hidden department never renders.
        ss = self._by_slug("seed-and-scion")
        ss.grove_dept_status = "hidden"
        nav = _catalog_nav(self.env, self.env.company)
        slugs = {d["slug"] for d in nav["departments"]}
        self.assertNotIn("forest-farming", slugs, "live-with-zero-products must be hidden")
        self.assertNotIn("seed-and-scion", slugs, "hidden department must never render")

    def test_product_department_derived_from_ancestry(self):
        restructure_department_tree(self.env)
        product = self.env["product.template"].create(
            {
                "name": "Chestnut Seedling",
                "website_published": True,
                "public_categ_ids": [(6, 0, [self.original["Nut Trees"]])],
            }
        )
        dept = _product_department(product)
        self.assertEqual(dept, {"slug": "orchard-food-forest", "name": "Orchard & food forest"})

    # ── notify-me waitlist tag ──────────────────────────────────────────

    def test_waitlist_resolver_maps_slug_to_department_name(self):
        restructure_department_tree(self.env)
        resolved = _resolve_waitlist_dept_names(self.env, ["waitlist:mycoforestry", "fruit"])
        self.assertEqual(resolved, {"mycoforestry": "Mycoforestry"})
        # End to end through the tag builder: readable "Waitlist: <Dept>" tag.
        tags = newsletter_tag_names("nursery", ["waitlist:mycoforestry"], waitlist_names=resolved)
        self.assertIn("Waitlist: Mycoforestry", tags)
