"""Pure helpers for the Mailgun webhook + outgoing-email tagging (GOL-2903).

Stdlib only — no Odoo imports — so it loads by file path in the fast pytest lane
(same pattern as ``stripe_gateway`` / ``shipment_email``) and its signature
verification and status-ordering logic are unit-tested without an Odoo DB.

Two jobs live here:

* **Tag** an outgoing message with an ``X-Mailgun-Variables`` header carrying our
  ``grove.email.log`` row id + order ref, so Mailgun echoes them back on every
  event and the webhook can match an event to the exact row it belongs to
  (robust even though Mailgun replaces the SMTP Message-Id with its own).
* **Verify + parse** an inbound webhook: constant-time HMAC check with a
  freshness window, then normalise Mailgun's event payload to our status vocab.
"""

import hashlib
import hmac
import json
import time

# Mailgun signs each webhook POST with HMAC-SHA256 over the concatenation
# "{timestamp}{token}" keyed by the account's HTTP webhook signing key. Reject
# anything older than this window to blunt replay / clock-skew (the caller is
# *additionally* responsible for rejecting a token it has already seen, which is
# the real single-use replay guard — see GroveEmailLog._reject_replayed_token).
SIG_TOLERANCE = 15 * 60  # seconds

# The customer-email kinds grove_headless can send. Every send site maps to
# exactly one of these so a filter/report can group by purpose. Kept here (not
# on the model) so a send site can reference it without importing the ORM.
EMAIL_KINDS = (
    "receipt",
    "deposit",
    "shipped",
    "out_for_delivery",
    "delivered",
    "dunning",
    "oversell_apology",
)

# Monotonic rank for a log row's delivery status. A status only ever moves UP
# this ladder, never down — that is what keeps a late "delivered" from
# clobbering an "opened" (GOL-2903 §2) and makes duplicate deliveries
# idempotent. A *temporary* failure is the one exception: it ranks low (see
# ``status_rank``) because Mailgun will retry and the message may still deliver.
STATUS_RANK = {
    "pending": 0,  # row created, not yet handed to Mailgun
    "sent": 10,  # handed to Mailgun by our SMTP server
    "delivered": 40,  # delivered to the recipient's MX
    "opened": 50,  # recipient opened (approximate — image-based)
    "blocked": 70,  # Mailgun refused to send (prior bounce/unsub/complaint)
    "failed": 80,  # permanent bounce / drop — terminal
    "complained": 90,  # spam complaint — always worth surfacing, so it wins
}

# Statuses ops needs to hear about the moment they land (§4 Discord alert).
ALERT_STATUSES = ("failed", "blocked", "complained")


class MailgunError(Exception):
    """Raised when a Mailgun webhook cannot be trusted or parsed."""


def verify_signature(signing_key, timestamp, token, signature, tolerance=SIG_TOLERANCE, now=None):
    """Verify a Mailgun webhook signature triple.

    Returns ``True`` on success; raises :class:`MailgunError` on any failure.
    Mailgun's scheme: ``HMAC-SHA256(key, "{timestamp}{token}")`` compared
    constant-time against the provided ``signature``, with a freshness window on
    the timestamp. ``timestamp``/``token``/``signature`` come from the request
    body's ``signature`` object (Mailgun posts JSON, not form fields, on the
    modern webhook).
    """
    if not signing_key:
        raise MailgunError("webhook signing key is not configured")
    if not (timestamp and token and signature):
        raise MailgunError("signature triple is incomplete")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise MailgunError("signature timestamp is not an integer") from exc
    if now is None:
        now = time.time()
    if tolerance and abs(now - ts) > tolerance:
        raise MailgunError("webhook timestamp is outside the tolerance window")
    signed = f"{timestamp}{token}".encode("utf-8")
    expected = hmac.new(signing_key.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(signature)):
        raise MailgunError("webhook signature mismatch")
    return True


def status_rank(status, severity=None):
    """Rank a status for the never-regress comparison.

    A *temporary* failure is a soft bounce / deferral: Mailgun will retry and the
    message may still be delivered, so it ranks below ``delivered`` on purpose —
    a subsequent "delivered" is allowed to supersede it. A permanent failure
    keeps its terminal rank so nothing short of a complaint overrides it.
    """
    if status == "failed" and severity == "temporary":
        return 20  # deferred — a retry may still deliver
    return STATUS_RANK.get(status, 0)


def variables_header(log_id, order_ref):
    """Build the ``X-Mailgun-Variables`` header value for an outgoing message.

    Mailgun strips this header and echoes its keys back under
    ``event-data.user-variables`` on every event for the message, so it is our
    durable match key from a webhook event to the originating log row.
    """
    return json.dumps({"grove_log_id": str(log_id), "order_ref": order_ref or ""})


def parse_event(payload):
    """Normalise a Mailgun webhook body to our vocabulary.

    ``payload`` is the decoded JSON body. Returns a dict with a ``status`` of one
    of our tracked statuses, or ``status=None`` for an event we accept (200) but
    do not record (e.g. ``accepted``, ``clicked``, ``unsubscribed``). Raises
    :class:`MailgunError` only on structurally broken input.
    """
    if not isinstance(payload, dict):
        raise MailgunError("webhook payload is not an object")
    data = payload.get("event-data") or {}
    if not isinstance(data, dict):
        raise MailgunError("event-data is not an object")

    event = (data.get("event") or "").lower()
    severity = (data.get("severity") or "").lower() or None
    reason = data.get("reason") or ""
    delivery = data.get("delivery-status") or {}
    # A readable failure message: Mailgun puts the SMTP detail in
    # delivery-status.message, falling back to the coarse reason.
    detail = ""
    if isinstance(delivery, dict):
        detail = delivery.get("message") or delivery.get("description") or ""
    failure_reason = detail or reason

    # Map Mailgun event → our status. A "failed" whose reason is a suppression
    # ("suppress-bounce" / "-unsubscribe" / "-complaint") is Mailgun refusing to
    # send at all because of a prior event → we surface that as "blocked", which
    # is operationally distinct from a fresh bounce (§2).
    if event == "delivered":
        status = "delivered"
    elif event == "opened":
        status = "opened"
    elif event == "complained":
        status = "complained"
    elif event in ("failed", "rejected", "permanent_fail", "temporary_fail"):
        status = "blocked" if str(reason).startswith("suppress") else "failed"
        # Mailgun's legacy event names carry severity in the name itself.
        if not severity:
            if event == "temporary_fail":
                severity = "temporary"
            elif event in ("permanent_fail", "rejected"):
                severity = "permanent"
    else:
        status = None  # accepted / clicked / unsubscribed / unknown — ack, no-op

    user_vars = data.get("user-variables") or {}
    log_id = None
    if isinstance(user_vars, dict):
        raw = user_vars.get("grove_log_id")
        if raw not in (None, ""):
            try:
                log_id = int(raw)
            except (TypeError, ValueError):
                log_id = None

    message = data.get("message") or {}
    headers = message.get("headers") if isinstance(message, dict) else {}
    mailgun_message_id = ""
    if isinstance(headers, dict):
        mailgun_message_id = headers.get("message-id") or ""

    return {
        "event": event,
        "status": status,
        "severity": severity,
        "failure_reason": failure_reason if status in ("failed", "blocked") else "",
        "recipient": data.get("recipient") or "",
        "mailgun_message_id": mailgun_message_id,
        "log_id": log_id,
        "order_ref": (user_vars.get("order_ref") or "") if isinstance(user_vars, dict) else "",
        "event_timestamp": data.get("timestamp"),
    }
