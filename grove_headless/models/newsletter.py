"""Pure helpers for the newsletter opt-in endpoint (GOL-221).

Kept free of any Odoo imports so the tag-naming logic can be unit-tested
standalone (``python3 -m pytest``) as well as under Odoo's ``--test-enable``
runner — same pattern as ``shipping_zones.py``. The controller in
``controllers/main.py`` imports :func:`newsletter_tag_names` from here.
"""

# A shop-coming-soon "notify me" capture arrives as this interest (GOL-2744):
# ``waitlist:<dept-slug>``. It becomes a human-readable ``Waitlist: <Department>``
# contact tag — filterable for the launch email — rather than the machine
# ``interest:waitlist:<slug>`` form, so Josh can pull the list without decoding
# slugs.
WAITLIST_INTEREST_PREFIX = "waitlist:"


def _humanize_slug(slug):
    """Fallback display name for a waitlist slug with no resolved department.

    ``forest-farming`` -> ``Forest Farming``. Only used when the controller
    could not map the slug to a real department name (unknown/typo slug).
    """
    return " ".join(part for part in slug.replace("_", "-").split("-") if part).title()


def newsletter_tag_names(brand, interests, waitlist_names=None):
    """Build the ordered, de-duplicated res.partner.category tag names for a
    newsletter opt-in.

    Every subscriber gets the ``newsletter`` marker tag; brand and each interest
    are namespaced (``brand:<x>``, ``interest:<x>``) so attribution reports can
    filter them unambiguously and they never collide with unrelated partner
    categories. Values are lower-cased and trimmed; blank/non-string entries are
    dropped. Order is stable: marker, brand, then interests in input order.

    A ``waitlist:<dept-slug>`` interest is special-cased into a readable
    ``Waitlist: <Department>`` tag (GOL-2744). ``waitlist_names`` optionally maps
    a dept slug to its exact department name (resolved Odoo-side by the caller);
    an unmapped slug falls back to a title-cased slug.
    """
    names = ["newsletter"]
    if isinstance(brand, str) and brand.strip():
        names.append(f"brand:{brand.strip().lower()}")
    waitlist_names = waitlist_names or {}
    seen = set(names)
    if isinstance(interests, (list, tuple)):
        for interest in interests:
            if not isinstance(interest, str):
                continue
            cleaned = interest.strip().lower()
            if not cleaned:
                continue
            if cleaned.startswith(WAITLIST_INTEREST_PREFIX):
                slug = cleaned[len(WAITLIST_INTEREST_PREFIX) :].strip()
                if not slug:
                    continue
                display = waitlist_names.get(slug) or _humanize_slug(slug)
                tag = f"Waitlist: {display}"
            else:
                tag = f"interest:{cleaned}"
            if tag not in seen:
                seen.add(tag)
                names.append(tag)
    return names
