"""Pure helpers for the newsletter opt-in endpoint (GOL-221).

Kept free of any Odoo imports so the tag-naming logic can be unit-tested
standalone (``python3 -m pytest``) as well as under Odoo's ``--test-enable``
runner — same pattern as ``shipping_zones.py``. The controller in
``controllers/main.py`` imports :func:`newsletter_tag_names` from here.
"""


def newsletter_tag_names(brand, interests):
    """Build the ordered, de-duplicated res.partner.category tag names for a
    newsletter opt-in.

    Every subscriber gets the ``newsletter`` marker tag; brand and each interest
    are namespaced (``brand:<x>``, ``interest:<x>``) so attribution reports can
    filter them unambiguously and they never collide with unrelated partner
    categories. Values are lower-cased and trimmed; blank/non-string entries are
    dropped. Order is stable: marker, brand, then interests in input order.
    """
    names = ["newsletter"]
    if isinstance(brand, str) and brand.strip():
        names.append(f"brand:{brand.strip().lower()}")
    seen = set(names)
    if isinstance(interests, (list, tuple)):
        for interest in interests:
            if not isinstance(interest, str):
                continue
            cleaned = interest.strip().lower()
            if not cleaned:
                continue
            tag = f"interest:{cleaned}"
            if tag not in seen:
                seen.add(tag)
                names.append(tag)
    return names
