"""Environment tag for every Discord post grove_headless sends.

Prod, QA and any restored copy of the database post through the same ops
webhook, and the posts carried no marker, so a stale QA/clone message was
indistinguishable from prod in the channel (2026-11-09: a Saturday prod audit
post was chased as a live incident across four environments). Every poster now
prefixes its message with ``[<host>]`` — the host of ``web.base.url``, the one
value each environment already sets differently via the entrypoint's
``seed_web_base_url`` — falling back to ``[db:<dbname>]`` when the parameter is
blank. ``env_tag_from`` is pure/stdlib so it unit-tests without the ORM;
grove_support carries its own copy because it must not import grove_headless.
"""

import logging
import os
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

# Topic-channel routing for every grove_headless Discord post. A channel key
# (or None for the default orders channel) resolves to its own webhook and
# otherwise falls back to the orders channel and finally the ops channel, so an
# unset topic webhook surfaces the alert (visibly misrouted) rather than
# dropping it. One fallback order shared by the controller and the live-quote
# alerts (GOL-4056).
DISCORD_CHANNEL_ENV = {
    "sales": "DISCORD_SALES_WEBHOOK_URL",
    "shipping": "DISCORD_SHIPPING_WEBHOOK_URL",
}


def discord_webhook_url(channel=None):
    """Webhook for ``channel`` (a DISCORD_CHANNEL_ENV key, or None for the
    orders channel), falling back to DISCORD_ORDERS_WEBHOOK_URL and then
    DISCORD_OPS_WEBHOOK_URL. "" when none is set."""
    env_name = DISCORD_CHANNEL_ENV.get(channel)
    return (
        (os.environ.get(env_name, "") if env_name else "")
        or os.environ.get("DISCORD_ORDERS_WEBHOOK_URL", "")
        or os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
    )


def env_tag_from(base_url, dbname):
    """``[host[:port]]`` of ``base_url``, else ``[db:<dbname>]``."""
    host = urlsplit(base_url or "").netloc
    return f"[{host}]" if host else f"[db:{dbname}]"


def discord_env_tag(env):
    """Tag for this Odoo instance. Never raises: a Discord post is best-effort
    and the tag must not be the reason an alert is lost."""
    try:
        base_url = env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return env_tag_from(base_url, env.cr.dbname)
    except Exception:  # noqa: BLE001 — tagging is best-effort
        _logger.warning("discord_tag: could not resolve the environment tag", exc_info=True)
        return "[grove]"


def tag_discord_message(env, message):
    """``"<tag> <message>"`` — callers truncate to Discord's 2000 chars after."""
    return f"{discord_env_tag(env)} {message}"
