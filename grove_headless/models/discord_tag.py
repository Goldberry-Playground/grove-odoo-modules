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
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)


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
