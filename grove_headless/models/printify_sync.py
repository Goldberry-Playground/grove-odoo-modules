"""Map a Printify product payload to the shape the Odoo sync writes (pure, no Odoo).
Printify prices are integer cents; options carry a ``type`` of ``color`` / ``size``."""

_AXIS_BY_TYPE = {"color": "Color", "size": "Size"}


def map_product(raw):
    value_axis = {}
    for option in raw.get("options") or []:
        axis = _AXIS_BY_TYPE.get((option.get("type") or "").lower())
        if not axis:
            continue
        for value in option.get("values") or []:
            value_axis[value["id"]] = (axis, value["title"])

    variants = []
    for variant in raw.get("variants") or []:
        axes = dict(value_axis[i] for i in variant.get("options") or [] if i in value_axis)
        variants.append(
            {
                "printify_variant_id": int(variant["id"]),
                "color": axes.get("Color", ""),
                "size": axes.get("Size", ""),
                "price": round((variant.get("price") or 0) / 100.0, 2),
                "cost": round((variant.get("cost") or 0) / 100.0, 2),
                "enabled": bool(variant.get("is_enabled")),
            }
        )

    images = raw.get("images") or []
    default = next((img["src"] for img in images if img.get("is_default")), None)
    return {
        "printify_product_id": str(raw["id"]),
        "name": raw.get("title") or "",
        "description": raw.get("description") or "",
        "image_url": default or (images[0]["src"] if images else None),
        "variants": variants,
    }
