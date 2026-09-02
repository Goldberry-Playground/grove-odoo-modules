"""Best-effort Discord ops ping for grove_support (GOL-2022).

Mirrors the ``_notify_discord`` pattern in grove_headless
``controllers/main.py`` (~2251): a missing ``DISCORD_OPS_WEBHOOK_URL`` or a
failed POST is swallowed so a chat event never fails because the ops channel is
unreachable. Kept in its own module (no ``odoo`` import) so the message
formatter stays trivially unit-testable and the network I/O is a single,
monkeypatchable seam in tests.
"""

import logging
import os

import requests

_logger = logging.getLogger(__name__)


def format_chat_start(*, brand=None, visitor=None):
    """One-line ops ping announcing a new livechat conversation."""
    who = visitor or "A visitor"
    where = f" ({brand})" if brand else ""
    return f"\U0001f4ac New support chat started{where}: {who}"


def notify_discord(message):
    """POST ``message`` to ``DISCORD_OPS_WEBHOOK_URL``.

    Best-effort: a missing URL or a failed request is logged and swallowed,
    never raised to the caller. ``allowed_mentions`` is disarmed so a visitor
    display name of ``@everyone``/``@here`` can never ping staff (same stance as
    grove_headless order alerts).
    """
    url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
    if not url:
        return
    try:
        requests.post(
            url,
            json={"content": message, "allowed_mentions": {"parse": []}},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — ops ping is best-effort
        _logger.warning("grove_support: Discord chat-start ping failed", exc_info=True)
