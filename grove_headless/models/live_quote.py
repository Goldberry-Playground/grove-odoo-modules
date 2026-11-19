"""Live-quote cache + alerting for the live-ZIP checkout (GOL-3927, spec §2 / §5).

Two ORM models and the thin glue that joins the pure Pirate Ship client
(``pirateship_rates``) and the pure fallback chain (``live_quote_chain``) to the
checkout:

* ``grove.shipping.live.quote`` — the live-quote cache (spec §2). Key
  ``(dest_zip, box_id, ceil(weight_lb))``, 24 h TTL, in the DB so it survives
  worker restarts. A cache hit never calls Pirate Ship; errors are never cached.
* ``grove.shipping.quote.event`` — one row per box quote outcome, so the rolling
  one-hour fallback-rate alert (spec §5: >20% fallbacks over the last hour, min 5
  quotes) can be computed, and the ``QueryRetired`` immediate/daily-deduped alert
  can fire. A retired persisted query also auto-disables the live flag.

Everything here is gated by ``grove_headless.zip_live_quotes_enabled`` (default
off). With the flag off the checkout never calls ``quote_box`` at all, so the
charge is the zone table byte-for-byte (spec §5, the step-1 acceptance check).
"""

import json
import logging
import math
import os
from datetime import timedelta

import requests
from odoo import api, fields, models

from . import discord_tag, live_quote_chain, pirateship_rates

_logger = logging.getLogger(__name__)

# Default-off feature flag (spec §5). An ir.config_parameter so Josh can flip it
# in Settings > Technical > System Parameters after promote without a deploy.
FLAG_PARAM = "grove_headless.zip_live_quotes_enabled"
# Bookkeeping params for the deduped alerts (not seeded; written at runtime).
_RETIRED_ALERT_DATE_PARAM = "grove_headless.zip_live_quotes_retired_alert_date"
_FALLBACK_ALERT_AT_PARAM = "grove_headless.zip_live_quotes_fallback_alert_at"

CACHE_TTL_HOURS = 24

# Rolling-window alert thresholds (spec §5 / Error handling).
_ALERT_WINDOW = timedelta(hours=1)
_ALERT_MIN_QUOTES = 5
_ALERT_FALLBACK_FRACTION = 0.20
# Don't re-post the fallback-rate alert more than once an hour (it would fire on
# every subsequent checkout while the window stays hot).
_ALERT_THROTTLE = timedelta(hours=1)

_TRUTHY = {"1", "true", "yes", "on"}


def live_quotes_enabled(env) -> bool:
    """True when the default-off live-quote flag is on."""
    raw = (env["ir.config_parameter"].sudo().get_param(FLAG_PARAM) or "").strip().lower()
    return raw in _TRUTHY


def _discord_ops_url() -> str:
    return os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")


def _post_discord(env, text) -> None:
    """Best-effort Discord ops post, environment-tagged. Never raises — an alert
    must not be the reason a checkout or cron fails."""
    msg = discord_tag.tag_discord_message(env, text)
    url = _discord_ops_url()
    if not url:
        _logger.info("live-quote alert: DISCORD_OPS_WEBHOOK_URL unset — %s", msg)
        return
    try:
        requests.post(url, json={"content": msg[:2000]}, timeout=10)
    except Exception:  # noqa: BLE001 — alerting is best-effort
        _logger.warning("live-quote alert: Discord notify failed", exc_info=True)


class GroveShippingLiveQuote(models.Model):
    """24 h cache of a single live Pirate Ship box quote (spec §2)."""

    _name = "grove.shipping.live.quote"
    _description = "Live Pirate Ship quote cache"
    _order = "fetched_at desc"

    dest_zip = fields.Char(required=True, index=True)
    box_id = fields.Char(required=True, index=True)
    # ceil(packed weight in lb) — part of the cache key so two carts that pack a
    # box to the same rounded-up weight share one quote (spec §2).
    weight_lb_ceil = fields.Integer(required=True, index=True)
    total = fields.Float(required=True)
    service = fields.Char()
    service_title = fields.Char()
    surcharges_json = fields.Text()
    transit_days = fields.Integer()
    fetched_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)

    _key_uniq = models.Constraint(
        "unique(dest_zip, box_id, weight_lb_ceil)",
        "A live quote is already cached for this ZIP / box / weight.",
    )

    @api.model
    def _key(self, dest_zip, box_id, weight_lb):
        return (str(dest_zip).strip(), str(box_id), int(math.ceil(float(weight_lb))))

    @api.model
    def lookup(self, dest_zip, box_id, weight_lb):
        """A non-expired cached quote as a ``live`` mapping for
        ``live_quote_chain.resolve_box``, or None on miss/expiry."""
        zip_, box, wc = self._key(dest_zip, box_id, weight_lb)
        cutoff = fields.Datetime.now() - timedelta(hours=CACHE_TTL_HOURS)
        row = self.sudo().search(
            [("dest_zip", "=", zip_), ("box_id", "=", box), ("weight_lb_ceil", "=", wc), ("fetched_at", ">=", cutoff)],
            limit=1,
        )
        if not row:
            return None
        return {"price": row.total, "service": row.service, "service_title": row.service_title}

    @api.model
    def remember(self, dest_zip, box_id, weight_lb, quote):
        """Upsert the cache row for ``quote`` (a ``pirateship_rates.Quote``)."""
        zip_, box, wc = self._key(dest_zip, box_id, weight_lb)
        vals = {
            "dest_zip": zip_,
            "box_id": box,
            "weight_lb_ceil": wc,
            "total": quote.total,
            "service": quote.winner.service,
            "service_title": quote.winner.service_title,
            "surcharges_json": json.dumps([{"name": s.name, "amount": s.amount} for s in quote.winner.surcharges]),
            "transit_days": quote.winner.transit_days or 0,
            "fetched_at": fields.Datetime.now(),
        }
        existing = self.sudo().search(
            [("dest_zip", "=", zip_), ("box_id", "=", box), ("weight_lb_ceil", "=", wc)], limit=1
        )
        if existing:
            existing.write(vals)
        else:
            self.sudo().create(vals)


class GroveShippingQuoteEvent(models.Model):
    """One checkout box-quote outcome, for the rolling fallback-rate alert."""

    _name = "grove.shipping.quote.event"
    _description = "Live checkout quote outcome"
    _order = "id desc"

    outcome = fields.Selection([("live", "Live"), ("fallback", "Fallback")], required=True, index=True)
    level = fields.Char()  # the chain level actually used: live / zip_area / zone
    reason = fields.Char()  # typed Pirate Ship error that caused the fallback
    dest_zip = fields.Char()
    box_id = fields.Char()
    order_id = fields.Many2one("sale.order", ondelete="set null", index=True)

    @api.model
    def log_order(self, order, dest_zip, box_quotes):
        """Record one event per resolved :class:`live_quote_chain.BoxQuote` and
        then evaluate the rolling-hour fallback alert."""
        for bq in box_quotes:
            self.sudo().create(
                {
                    "outcome": "live" if bq.source == live_quote_chain.SOURCE_LIVE else "fallback",
                    "level": bq.source,
                    "reason": (bq.reason or "")[:200] or False,
                    "dest_zip": str(dest_zip),
                    "box_id": bq.box_id,
                    "order_id": order.id if order else False,
                }
            )
        self._maybe_alert_fallback_rate()

    @api.model
    def _maybe_alert_fallback_rate(self):
        """Discord when >20% of the last hour's quotes fell back (min 5), throttled
        to at most one post per hour."""
        now = fields.Datetime.now()
        recent = self.sudo().search([("create_date", ">=", now - _ALERT_WINDOW)])
        total = len(recent)
        if total < _ALERT_MIN_QUOTES:
            return
        fallbacks = len(recent.filtered(lambda e: e.outcome == "fallback"))
        if fallbacks / total <= _ALERT_FALLBACK_FRACTION:
            return
        icp = self.env["ir.config_parameter"].sudo()
        last_raw = icp.get_param(_FALLBACK_ALERT_AT_PARAM) or ""
        if last_raw:
            try:
                if fields.Datetime.to_datetime(last_raw) > now - _ALERT_THROTTLE:
                    return
            except (ValueError, TypeError):
                pass
        icp.set_param(_FALLBACK_ALERT_AT_PARAM, fields.Datetime.to_string(now))
        pct = round(100.0 * fallbacks / total)
        _post_discord(
            self.env,
            f"Live shipping quotes: {fallbacks}/{total} ({pct}%) fell back to the ZIP-area/zone table "
            f"in the last hour (threshold 20%, min 5). Pirate Ship may be rate-limiting or down.",
        )

    @api.model
    def alert_query_retired(self, reason=""):
        """A retired persisted query: disable the live flag and Discord-alert
        immediately, deduped to once per day (spec Error handling)."""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(FLAG_PARAM, "0")
        today = fields.Date.to_string(fields.Date.context_today(self))
        if icp.get_param(_RETIRED_ALERT_DATE_PARAM) == today:
            return
        icp.set_param(_RETIRED_ALERT_DATE_PARAM, today)
        _post_discord(
            self.env,
            "Live shipping quotes DISABLED: Pirate Ship no longer recognizes our persisted query "
            f"(hash retired upstream). Update pirateship_rates.PERSISTED_QUERY_HASH and re-enable. {reason}".strip(),
        )


def quote_box(env, dest_zip, box_id, weight_lb):
    """Live price for one box: cache -> Pirate Ship. Returns the ``live`` mapping
    ``live_quote_chain.resolve_box`` expects — ``{"price", "service",
    "service_title"}`` on a hit, ``{"reason": "<error>"}`` on a typed failure.

    A :class:`pirateship_rates.QueryRetired` disables the whole live path (flag
    off) and alerts before returning its reason, so the rest of the cart (and
    every later checkout until the hash is fixed) goes straight to the fallback.
    Errors are never cached (spec §2)."""
    Cache = env["grove.shipping.live.quote"].sudo()
    cached = Cache.lookup(dest_zip, box_id, weight_lb)
    if cached is not None:
        return cached
    try:
        q = pirateship_rates.quote(dest_zip, box_id, weight_lb)
    except pirateship_rates.QueryRetired as exc:
        env["grove.shipping.quote.event"].sudo().alert_query_retired(str(exc))
        return {"reason": "QueryRetired"}
    except pirateship_rates.PirateShipError as exc:
        return {"reason": type(exc).__name__}
    Cache.remember(dest_zip, box_id, weight_lb, q)
    return {"price": q.total, "service": q.service, "service_title": q.service_title}
