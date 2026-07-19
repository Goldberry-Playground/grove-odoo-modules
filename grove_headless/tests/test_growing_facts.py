"""Growing-facts fields (2026-07-13 catalog spec). DB tests — Odoo runner only."""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGrowingFacts(TransactionCase):
    def _tmpl(self, **vals):
        base = {"name": "Test Pear", "type": "consu"}
        base.update(vals)
        return self.env["product.template"].create(base)

    def test_facts_fields_exist_and_store(self):
        t = self._tmpl(
            grove_botanical_name="Pyrus communis",
            grove_zone_min=5,
            grove_zone_max=8,
            grove_layer="canopy",
            grove_sun="full",
            grove_mature_size="15-20 ft x 12 ft",
            grove_spacing="15 ft",
            grove_soil="well-drained",
        )
        self.assertEqual(t.grove_botanical_name, "Pyrus communis")
        self.assertEqual((t.grove_zone_min, t.grove_zone_max), (5, 8))
        self.assertEqual(t.grove_layer, "canopy")
        self.assertEqual(t.grove_sun, "full")

    def test_zone_range_constraint(self):
        with self.assertRaises(ValidationError):
            self._tmpl(grove_zone_min=8, grove_zone_max=5)

    def test_zones_optional(self):
        t = self._tmpl()
        self.assertFalse(t.grove_zone_min)
        self.assertFalse(t.grove_botanical_name)
