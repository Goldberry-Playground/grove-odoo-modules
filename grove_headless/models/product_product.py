import logging

from odoo import _, api, fields, models

from . import capture_leads
from .mail_from import mail_from_vals

_logger = logging.getLogger(__name__)

FORMAT_ATTRIBUTE = "Format"
BAREROOT_VALUE = "Bareroot"
POTTED_VALUE = "Potted"


class ProductProduct(models.Model):
    _inherit = "product.product"

    # Printify variant id (one per size/color), set by the Printify sync
    # (spec 2026-10-13-printify-merch-design.md §3). Empty for every in-house product.
    printify_variant_id = fields.Integer(string="Printify variant ID", index=True, copy=False, readonly=True)

    # The zone-rate engine bills bareroot as a 4 lb slim box and potted as a
    # ~25 lb box. Format is a VARIANT axis on live data, so the tier must be
    # resolved per-variant — the template field alone quotes bareroot pears
    # at potted rates (bug found in the 2026-07-13 design review).
    grove_effective_shipping_tier = fields.Selection(
        [("bareroot", "Bareroot"), ("potted", "Potted"), ("seed", "Seed")],
        compute="_compute_grove_effective_shipping_tier",
        string="Effective Shipping Tier",
    )

    # Pack weight (lb) for a seed pre-order variant's "Pack size" axis value.
    # Counts against the template's season cap (grove_seed_cap_lb) by pack
    # weight × qty, and is the per-line weight re-checked at checkout. 0 for
    # non-seed variants (GOL-3257).
    grove_seed_pack_lb = fields.Float(string="Seed Pack Weight (lb)")

    @api.depends("product_template_variant_value_ids", "product_tmpl_id.grove_shipping_tier")
    def _compute_grove_effective_shipping_tier(self):
        for product in self:
            fmt_values = product.product_template_variant_value_ids.filtered(
                lambda v: v.attribute_id.name == FORMAT_ATTRIBUTE
            )
            if fmt_values and fmt_values[0].name == BAREROOT_VALUE:
                product.grove_effective_shipping_tier = "bareroot"
            elif fmt_values and fmt_values[0].name == POTTED_VALUE:
                # Symmetric override (Josh 2026-09-06): a Potted variant on a
                # template whose tier is "bareroot" (apples, pears) inherited
                # "bareroot" via the fallback below, so the storefront badged it
                # "Peat & bagged" and quoted shipping — Potted is pickup-only.
                product.grove_effective_shipping_tier = "potted"
            else:
                product.grove_effective_shipping_tier = product.product_tmpl_id.grove_shipping_tier or "potted"

    def _grove_potted_pool_siblings(self):
        """Variants sharing this bareroot variant's PHYSICAL stock pool.

        Peat & bagged (GOL-2031): the trees that ship as "Bareroot" during the
        leafed season ARE the potted stock — staff pull the tree from its pot
        and bag the root ball in damp peat at packing (Josh 2026-09-06). So a
        Bareroot variant's sellable pool must include its non-Bareroot sibling:
        same template, identical on every axis except Format. Empty for
        non-bareroot variants and for templates with no Format axis.
        """
        self.ensure_one()
        if self.grove_effective_shipping_tier != "bareroot":
            return self.browse()

        def non_format(variant):
            return variant.product_template_variant_value_ids.filtered(
                lambda v: v.attribute_id.name != FORMAT_ATTRIBUTE
            )

        mine = non_format(self)
        return self.product_tmpl_id.product_variant_ids.filtered(
            lambda s: s.id != self.id and s.grove_effective_shipping_tier != "bareroot" and non_format(s) == mine
        )

    def grove_shared_pool_qty(self, field="free_qty"):
        """Sellable quantity for this variant including its shared pool.

        ``field`` is "free_qty" for the money paths (checkout deposit gate,
        webhook oversell check) and "qty_available" for the PDP display. The
        ships-now vs preorder question stays with the shipping calendar — this
        only answers "how many trees back this option". Company context is the
        caller's: invoke on a ``with_company()`` record where it matters.
        """
        self.ensure_one()
        return self[field] + sum(s[field] for s in self._grove_potted_pool_siblings())

    # ── Back-in-stock alerts (2026-10-15 capture hotfix) ────────────────────
    # website_sale_stock's hourly cron judges stock against the *current*
    # website (the farm, in a cron) and links to Odoo's own /shop. On the
    # headless multi-company storefronts that means nursery alerts never fire
    # and, when they do, link and send from the wrong brand. Override it to:
    # judge each product against its own company (incl. the bareroot/potted
    # shared pool), only for published + sellable products, send ONE email per
    # template, from the company's from_filter-compliant sender, linking to the
    # storefront PDP.

    def _grove_restock_website(self):
        self.ensure_one()
        Website = self.env["website"].sudo()
        return (
            Website.search([("company_id", "=", self.company_id.id)], limit=1) if self.company_id else Website.browse()
        )

    def _grove_restock_available(self):
        self.ensure_one()
        tmpl = self.product_tmpl_id
        if not (self.active and tmpl.sale_ok and tmpl.website_published):
            return False
        product = self.with_company(self.company_id) if self.company_id else self
        return product.grove_shared_pool_qty("free_qty") > 0

    def _grove_send_restock_email(self, partner, website):
        self.ensure_one()
        product = self.with_context(lang=partner.lang)
        url = capture_leads.storefront_product_url(website.domain if website else "", self.product_tmpl_id.id)
        body_html = self.env["ir.qweb"]._render(
            "website_sale_stock.availability_email_body",
            {"product": product, "storefront_url": url},
        )
        full_mail = product.env["mail.render.mixin"]._render_encapsulate(
            "mail.mail_notification_light",
            body_html,
            add_context={"model_description": _("Product")},
            context_record=product,
        )
        company = (website.company_id if website else False) or self.company_id or self.env.company
        mail = (
            self.env["mail.mail"]
            .sudo()
            .create(
                {
                    "subject": _("%(product_name)s is back in stock", product_name=product.product_tmpl_id.name),
                    "email_to": partner.email_formatted,
                    "body_html": full_mail,
                    **mail_from_vals(self.env, company),
                }
            )
        )
        mail.send(raise_exception=False)

    @api.model
    def _send_availability_email(self):
        notified = set()  # (partner id, template id): one email per template per run
        for product in self.sudo().search([("stock_notification_partner_ids", "!=", False)]):
            try:
                if not product._grove_restock_available():
                    continue
                website = product._grove_restock_website()
                partners = product.stock_notification_partner_ids
                for partner in partners:
                    key = (partner.id, product.product_tmpl_id.id)
                    if key in notified:
                        continue
                    product._grove_send_restock_email(partner, website)
                    notified.add(key)
                # They asked about the plant, not one Format: clear the whole template.
                siblings = product.product_tmpl_id.with_context(active_test=False).product_variant_ids
                siblings.write({"stock_notification_partner_ids": [(3, p.id) for p in partners]})
            except Exception:  # noqa: BLE001 — one bad product must not stop the cron
                _logger.exception("grove_headless: back-in-stock email failed for product %s", product.id)
