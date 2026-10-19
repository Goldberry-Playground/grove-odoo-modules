"""Storefront capture routing: which form becomes which CRM lead (2026-10-15 hotfix).

Josh ruled: consult / wholesale / farm-visit / new-state-waitlist requests are
customer relationships, so they land in Odoo CRM as leads; back-in-stock
alerts use Odoo's own stock notifications; only the newsletter stays in Ghost.

Pure (no Odoo, no relative imports) so tests load it by path.
"""

import re

EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")

# kind -> human label (also the CRM tag every lead of that kind carries).
LEAD_KINDS = {
    "food_forest_consult": "Food forest consult",
    "wholesale": "Wholesale inquiry",
    "farm_visit": "Farm visit request",
    "state_waitlist": "New-state waitlist",
}

MAX_EMAIL = 254
MAX_NAME = 200
MAX_PHONE = 30
MAX_MESSAGE = 2000
MAX_LABEL = 100
MAX_STATE = 2


def _text(payload, key):
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def validate_lead(payload):
    """Return ``(clean, None)`` or ``(None, error)`` for a lead capture body."""
    if not isinstance(payload, dict):
        return None, "Invalid JSON body"
    email = _text(payload, "email")
    if not email:
        return None, "email is required"
    if len(email) > MAX_EMAIL or not EMAIL_RE.fullmatch(email):
        return None, "email is not a valid email address"
    kind = _text(payload, "kind")
    if kind not in LEAD_KINDS:
        return None, f"kind must be one of {', '.join(sorted(LEAD_KINDS))}"

    clean = {
        "email": email,
        "kind": kind,
        "name": _text(payload, "name"),
        "phone": _text(payload, "phone"),
        "message": _text(payload, "message"),
        "label": _text(payload, "label"),
        "state": _text(payload, "state").upper(),
        "product_id": None,
    }
    for key, limit in (("name", MAX_NAME), ("phone", MAX_PHONE), ("message", MAX_MESSAGE), ("label", MAX_LABEL)):
        if len(clean[key]) > limit:
            return None, f"{key} exceeds {limit} characters"
    if clean["state"] and (len(clean["state"]) != MAX_STATE or not clean["state"].isalpha()):
        return None, "state must be a two-letter state code"
    if kind == "state_waitlist" and not clean["state"]:
        return None, "state is required for state_waitlist"

    raw_product = payload.get("product_id")
    if raw_product not in (None, ""):
        try:
            clean["product_id"] = int(raw_product)
        except (TypeError, ValueError):
            return None, "product_id must be an integer"
    return clean, None


def lead_title(kind, name, email, product_name=None, state=None):
    title = f"{LEAD_KINDS[kind]}: {name or email}"
    context = product_name or state
    return f"{title} ({context})" if context else title


def lead_tag_names(kind, label):
    """The kind's tag, plus a ``<kind>:<label>`` tag when the form names itself
    (e.g. farm visits split into upick / seminars / private)."""
    tags = [LEAD_KINDS[kind]]
    if label:
        tags.append(f"{kind.replace('_', '-')}:{label.lower()}")
    return tags


def lead_description(clean, source=""):
    lines = []
    if clean.get("message"):
        lines += [clean["message"], ""]
    if clean.get("label"):
        lines.append(f"Form: {clean['label']}")
    if clean.get("state"):
        lines.append(f"State: {clean['state']}")
    if clean.get("phone"):
        lines.append(f"Phone: {clean['phone']}")
    if source:
        lines.append(f"Captured from: {source}")
    return "\n".join(lines).strip()


def storefront_product_url(domain, template_id):
    """The storefront PDP for a product template (``/shop/<template id>``), or
    None when the website has no domain configured."""
    if not domain:
        return None
    base = domain.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return f"{base}/shop/{int(template_id)}"
