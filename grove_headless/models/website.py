import logging

from odoo import api, models
from odoo.http import request

_logger = logging.getLogger(__name__)

# Tenant slug → website name mapping for human-readable header values.
# These must match the website record names in the database.
# TODO: Replace with a grove_tenant_slug field on website model once
#       the setup stabilizes (see P2 backlog).
_TENANT_SLUGS = {
    "goldberry": "Goldberry Grove Farm",
    "ggg": "George George George Woodworking",
    "nursery": "At The Grove Nursery",
}

# Reverse map for resolving a website back to its slug (publish webhook routing).
_SLUG_BY_NAME = {name: slug for slug, name in _TENANT_SLUGS.items()}


class Website(models.Model):
    _inherit = "website"

    @api.model
    def get_current_website(self, fallback=True):
        """Allow X-Grove-Tenant header to override website resolution.

        Accepts a tenant slug (goldberry, ggg, nursery). Falls through
        to normal Host-based resolution when the header is absent or
        invalid.

        Production-safe: the header only selects which published
        catalog is returned. Record rules and company_id filters
        still enforce data isolation. In production, nginx can strip
        the header from external traffic if desired.
        """
        if request:
            tenant = request.httprequest.headers.get("X-Grove-Tenant")
            if tenant:
                website = self._resolve_tenant_slug(tenant)
                if website:
                    return website
                _logger.warning(
                    "X-Grove-Tenant=%s: could not resolve, falling through to Host routing",
                    tenant,
                )
        return super().get_current_website(fallback=fallback)

    def _resolve_tenant_slug(self, slug):
        """Resolve a tenant slug to an active website record.

        Only accepts known slug values from _TENANT_SLUGS — does NOT
        accept raw integer IDs to prevent probing of arbitrary records.
        """
        website_name = _TENANT_SLUGS.get(slug.strip().lower())
        if not website_name:
            return None

        # Use sudo to avoid access-rights issues during early routing
        # (get_current_website is called in _match before auth is resolved)
        website = self.sudo().search(
            [("name", "=", website_name)],
            limit=1,
        )
        return website or None

    def grove_tenant_slug(self):
        """Return this website's tenant slug (goldberry / ggg / nursery), or None.

        Inverse of `_resolve_tenant_slug`. Used by the publish webhook to route
        an event to the right tenant's grove-sites deployment. Name-based like
        the rest of the tenant mapping (see the TODO above about the field).
        """
        self.ensure_one()
        return _SLUG_BY_NAME.get(self.name)
