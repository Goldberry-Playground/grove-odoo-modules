"""grove.planner.plan — a saved plan and the staff action that turns it into a
guild (GOL-3549, planner winter beta spec B.7).

``POST /grove/api/v1/planner/save`` (public) stores a named plan with its lines.
Staff review saved plans in a list view; **Approve as guild** (restricted to
nursery staff) creates a phantom-BoM bundle template in the Guilds website
category — name from the plan, price the sum of the lines, unpublished — and
links it back on the plan. Publishing it is the existing publish gate's job. A
plan that reaches checkout keeps the sale order's own ``grove_planner_plan``
JSON; the two are not linked in v1.
"""

from odoo import fields, models
from odoo.exceptions import AccessError, UserError


class GrovePlannerPlan(models.Model):
    _name = "grove.planner.plan"
    _description = "Saved planner plan"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Name", required=True)
    plan_url = fields.Char(string="Plan URL")
    zone = fields.Integer(string="USDA Zone")
    track = fields.Char(string="Track")
    status = fields.Selection(
        [("saved", "Saved"), ("guild", "Approved as guild")],
        string="Status",
        default="saved",
        required=True,
        index=True,
    )
    line_ids = fields.One2many("grove.planner.plan.line", "plan_id", string="Lines")
    guild_product_id = fields.Many2one("product.template", string="Guild Bundle", readonly=True, ondelete="set null")
    # Stored solely for the per-browser-id rate limit on the public save route;
    # not shown in a view.
    browser_id = fields.Char(string="Browser Id", index=True)

    def action_approve_as_guild(self):
        """Create a Guilds-category phantom-BoM bundle from the plan's lines.

        Nursery-staff only. Price is the sum of line qty × list price; the
        bundle is created unpublished so the publish gate stays the one place a
        listing goes live (spec B.7).
        """
        if not self.env.user.has_group("grove_headless.group_nursery_staff"):
            raise AccessError("Only nursery staff may approve a plan as a guild.")

        collection = (
            self.env["product.public.category"].sudo().search([("grove_node_kind", "=", "collection")], limit=1)
        )
        if not collection:
            raise UserError("No Guilds collection category exists to file the bundle under.")

        Product = self.env["product.template"].sudo()
        Bom = self.env["mrp.bom"].sudo()
        for plan in self:
            if plan.status == "guild":
                continue
            lines = plan.line_ids.filtered(lambda line: line.product_tmpl_id)
            if not lines:
                raise UserError(f"Plan {plan.display_name} has no lines to bundle.")
            price = sum(line.qty * (line.product_tmpl_id.list_price or 0.0) for line in lines)
            bundle = Product.create(
                {
                    "name": plan.name,
                    "type": "consu",
                    "list_price": price,
                    "website_published": False,
                    "public_categ_ids": [(6, 0, [collection.id])],
                }
            )
            Bom.create(
                {
                    "product_tmpl_id": bundle.id,
                    "product_id": bundle.product_variant_id.id,
                    "type": "phantom",
                    "product_qty": 1.0,
                    "bom_line_ids": [
                        (0, 0, {"product_id": line.product_tmpl_id.product_variant_id.id, "product_qty": line.qty})
                        for line in lines
                    ],
                }
            )
            plan.write({"status": "guild", "guild_product_id": bundle.id})


class GrovePlannerPlanLine(models.Model):
    _name = "grove.planner.plan.line"
    _description = "Saved planner plan line"
    _order = "id"

    plan_id = fields.Many2one("grove.planner.plan", string="Plan", required=True, ondelete="cascade")
    product_tmpl_id = fields.Many2one("product.template", string="Product", required=True, ondelete="cascade")
    qty = fields.Float(string="Qty", default=1.0)
    role = fields.Char(string="Role")
    stratum = fields.Char(string="Stratum")
