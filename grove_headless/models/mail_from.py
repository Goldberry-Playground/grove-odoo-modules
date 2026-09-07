"""Shared ``email_from`` resolver for outgoing grove_headless mail (GOL-2180).

Every ``mail.mail`` created by grove_headless (weekly rollup digest, order
alerts, dunning/deposit/apology/shipping customer mail) must set ``email_from``
explicitly.  Without it Odoo falls back to the cron/request author identity
(``"OdooBot" <odoobot@example.com>``), which fails the prod outgoing server's
``from_filter`` (``send.gatheringatthegrove.com``) and risks Mailgun
rejection or spam-foldering.

The priority logic is pure/stdlib in ``order_digest.resolve_email_from`` so it
unit-tests without the Odoo runtime; this thin wrapper reads the relevant
``ir.config_parameter`` values, the company email, and the odoo.conf
``email_from`` (the deployment's versioned from_filter-compliant sender).
"""

from odoo.tools import config

from . import order_digest as od


def resolve_mail_from(env, company=None):
    """Return a from_filter-compliant ``email_from`` string, or ``None`` when
    nothing is configured (callers then omit ``email_from`` and defer to Odoo).

    Resolves ``mail.default.from`` -> ``company.email`` ->
    ``<mail.catchall.alias>@<mail.catchall.domain>`` -> odoo.conf ``email_from``
    (see ``order_digest.resolve_email_from``).
    """
    icp = env["ir.config_parameter"].sudo()
    company_email = company.email if company is not None else None
    return (
        od.resolve_email_from(
            icp.get_param("mail.default.from"),
            company_email,
            icp.get_param("mail.catchall.alias"),
            icp.get_param("mail.catchall.domain"),
            config.get("email_from"),
        )
        or None
    )


def mail_from_vals(env, company=None):
    """``mail.mail.create`` fragment: ``{"email_from": <resolved>}`` when a
    from_filter-compliant sender is configured, else ``{}``.

    Splice with ``**mail_from_vals(env, company)`` so the key is OMITTED when
    unresolved.  Passing ``email_from=None`` explicitly instead would persist a
    falsy From -- ``create()`` only applies field defaults for ABSENT keys, so
    Odoo would NOT fill its author default and the send would fail (the mail is
    silently dropped by the caller's best-effort ``try/except``).  Omitting the
    key defers to Odoo's author default, matching the pre-fix behaviour.
    """
    email_from = resolve_mail_from(env, company)
    return {"email_from": email_from} if email_from else {}
