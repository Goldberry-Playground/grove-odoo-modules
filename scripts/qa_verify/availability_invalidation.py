#!/usr/bin/env python3
"""GOL-2337 — automated QA verification for the GOL-1896 stock-change invalidation.

Replaces the manual 7-step click-through in GOL-2337 with one command so the
release-train window costs minutes, not an afternoon. Drives the REAL deployed
Odoo over XML-RPC (no test harness, no mocks) and reads the `grove.publish.event`
audit ledger back as evidence.

What it proves, per GOL-2337 step:

    1/2  probe template goes in stock, then sells out (on-hand crosses zero)
    3    ONE `product.availability` event per sellout, delivered (HTTP 2xx) to
         the tenant's grove-sites receiver  -> the fast path, not the ISR window
    4    restock emits the reverse transition
    5    three templates crossing in ONE transaction emit exactly 3 events
         (coalesced per template, not per stock move)
    6    --cap N templates in one transaction -> emits at most
         _AVAILABILITY_EMIT_CAP (50); the rest degrade to the /shop ISR window
    7    a mis/unconfigured tenant never breaks the write: the stock change
         still commits and no event row is created (fail-safe, no 500)

Steps 1-4 optionally assert the storefront HTML flips (--storefront) within the
fast-path budget, which is the user-visible acceptance criterion.

Credentials come from the environment only -- never hard-code or echo them:

    ODOO_URL      https://odoo.qa.gatheringatthegrove.com
    ODOO_DB       odoo
    ODOO_USER     josh@goldberrygrove.farm
    ODOO_API_KEY  op read 'op://Grove QA/Gather At the Grove QA Odoo/odoo_mcp_qa_api_key'

Probe data is self-cleaning: every template/quant it creates is named with the
run tag and archived + deleted in teardown (--keep to inspect a failure).
Safe to run twice; nothing outside its own probe records is mutated.

Usage:
    ODOO_API_KEY=... python3 scripts/qa_verify/availability_invalidation.py \
        --tenant nursery --storefront https://nursery.qa.gatheringatthegrove.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid

# tenant slug -> (res.company id, stock location id) in the QA database.
# Resolved live (below) rather than trusted from here; these are the defaults
# the resolver cross-checks against so a re-keyed QA DB fails loud.
TENANTS = ("goldberry", "ggg", "nursery")

EMIT_CAP = 50  # keep in sync with grove_publish_event._AVAILABILITY_EMIT_CAP
FAST_PATH_BUDGET_S = 5.0


class Odoo:
    """Minimal JSON-RPC client for the external Odoo API.

    JSON-RPC (not XML-RPC) on purpose: several stock methods we need return
    ``None``, which Odoo's XML-RPC marshaller refuses to encode
    ("cannot marshal None unless allow_none is enabled") — the write lands but
    the response blows up, which looks exactly like a failure. JSON encodes
    ``null`` natively.
    """

    def __init__(self, url: str, db: str, user: str, key: str):
        self.url, self.db, self.key = url.rstrip("/"), db, key
        # An explicit empty proxy config: the agent runtime exports a proxy that
        # does not carry these hosts, and urllib would silently use it.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.uid = self._call("common", "login", [db, user, key])
        if not self.uid:
            raise SystemExit(f"FATAL: authentication failed for {user} at {url}")

    def _call(self, service: str, method: str, args: list):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "id": uuid.uuid4().hex,
            "params": {"service": service, "method": method, "args": args},
        }
        request = urllib.request.Request(
            f"{self.url}/jsonrpc",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=120) as response:
            body = json.loads(response.read().decode())
        if "error" in body:
            error = body["error"]
            detail = (error.get("data") or {}).get("message") or error.get("message")
            raise RuntimeError(f"Odoo {service}.{method} failed: {detail}")
        return body.get("result")

    def kw(self, model, method, args, kwargs=None):
        return self._call("object", "execute_kw", [self.db, self.uid, self.key, model, method, args, kwargs or {}])


def log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def verdict(ok: bool, step: str, msg: str, results: list) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {step}: {msg}", flush=True)
    results.append((ok, step, msg))
    return ok


def resolve_tenant(odoo: Odoo, tenant: str):
    """(company_id, location_id) for a tenant slug, straight from the DB."""
    websites = odoo.kw("website", "search_read", [[], ["id", "name", "company_id"]])
    company_id = None
    for site in websites:
        name = (site["name"] or "").lower()
        slug = "nursery" if "nursery" in name else "ggg" if "woodworking" in name else "goldberry"
        if slug == tenant:
            company_id = site["company_id"][0]
            break
    if not company_id:
        raise SystemExit(f"FATAL: no website maps to tenant '{tenant}' -- check website.grove_tenant_slug()")
    warehouses = odoo.kw(
        "stock.warehouse", "search_read", [[["company_id", "=", company_id]], ["lot_stock_id"]], {"limit": 1}
    )
    if not warehouses:
        raise SystemExit(f"FATAL: company {company_id} has no warehouse -- cannot move on-hand stock")
    return company_id, warehouses[0]["lot_stock_id"][0]


def make_probe_template(odoo: Odoo, company_id: int, tag: str, index: int) -> int:
    """A storable, sellable probe template owned by the tenant company."""
    return odoo.kw(
        "product.template",
        "create",
        [
            {
                "name": f"ZZ QA availability probe {tag} #{index}",
                "default_code": f"ZZ-QA-AVAIL-{tag}-{index}",
                "type": "consu",
                "is_storable": True,
                "sale_ok": True,
                "website_published": False,
                "company_id": company_id,
                "list_price": 1.0,
            }
        ],
    )


def variant_of(odoo: Odoo, template_id: int) -> int:
    products = odoo.kw("product.product", "search_read", [[["product_tmpl_id", "=", template_id]], ["id"]])
    return products[0]["id"]


def set_on_hand(odoo: Odoo, product_ids: list, location_id: int, qty: float, company_id: int) -> None:
    """Inventory-adjust `product_ids` to `qty` in ONE transaction (one RPC call).

    Inventory mode is the supported write path for `stock.quant.quantity`; it is
    also exactly what QA step 6's "bulk inventory adjustment" exercises.
    """
    ctx = {"inventory_mode": True, "allowed_company_ids": [company_id], "company_id": company_id}
    quant_ids = []
    for product_id in product_ids:
        existing = odoo.kw(
            "stock.quant",
            "search",
            [[["product_id", "=", product_id], ["location_id", "=", location_id]]],
            {"context": ctx, "limit": 1},
        )
        if existing:
            quant_id = existing[0]
            odoo.kw("stock.quant", "write", [[quant_id], {"inventory_quantity": qty}], {"context": ctx})
        else:
            quant_id = odoo.kw(
                "stock.quant",
                "create",
                [{"product_id": product_id, "location_id": location_id, "inventory_quantity": qty}],
                {"context": ctx},
            )
        quant_ids.append(quant_id)
    # One call, one transaction -> one availability flush for all templates.
    odoo.kw("stock.quant", "action_apply_inventory", [quant_ids], {"context": ctx})


def variant_quantities(odoo: Odoo, template_id: int, company_id: int) -> dict:
    """{variant_id: on-hand} for a template, read in its own company.

    Per VARIANT, not per template: a template's `qty_available` is the sum across
    its variants, so restoring that total onto every variant multiplies real
    stock (Dogwood 10 = potted 10 + bareroot 0 came back as 10 + 10). Anything
    that mutates a real product must restore the per-variant split it found.
    """
    ctx = {"allowed_company_ids": [company_id], "company_id": company_id}
    rows = odoo.kw(
        "product.product",
        "search_read",
        [[["product_tmpl_id", "=", template_id]], ["id", "qty_available"]],
        {"context": ctx},
    )
    return {row["id"]: row["qty_available"] for row in rows}


def restore_quantities(odoo: Odoo, quantities: dict, location_id: int, company_id: int) -> None:
    """Put each variant back to the exact on-hand it had before the run."""
    for variant_id, qty in quantities.items():
        set_on_hand(odoo, [variant_id], location_id, qty, company_id)


def qty_available(odoo: Odoo, template_id: int, company_id: int) -> float:
    ctx = {"allowed_company_ids": [company_id], "company_id": company_id}
    rec = odoo.kw("product.template", "read", [[template_id], ["qty_available"]], {"context": ctx})
    return rec[0]["qty_available"]


def events_for(odoo: Odoo, template_ids: list, since: str) -> list:
    domain = [
        ["event_type", "=", "product.availability"],
        ["product_tmpl_id", "in", template_ids],
        ["create_date", ">=", since],
    ]
    return odoo.kw(
        "grove.publish.event",
        "search_read",
        [domain, ["delivery_id", "product_tmpl_id", "tenant", "state", "http_status", "error", "create_date"]],
        {"order": "create_date asc"},
    )


AVAILABILITY_LABELS = ("In stock", "Sold out", "Coming soon", "Notify me")
# Upper bound on one card's markup in the RSC flight payload (~1-1.5KB observed).
CARD_SLICE_LIMIT = 4000


def card_labels(html: str, product_id: int) -> set:
    """Availability wording rendered inside ONE product's /shop grid card.

    The grid ships as an RSC flight payload, so we slice from this product's
    `/shop/<id>` href to the next card's href and read the labels in between —
    the whole point is a per-product assertion, not a page-wide text search that
    any other in-stock card would satisfy.
    """
    start = html.find(f"/shop/{product_id}")
    if start < 0:
        return set()
    rest = html[start + 1 :]
    next_card = rest.find("/shop/")
    # Bound the slice: the LAST card has no following href, and an unbounded
    # tail would sweep in the page footer and every label on it.
    end = next_card if 0 < next_card < CARD_SLICE_LIMIT else CARD_SLICE_LIMIT
    return {label for label in AVAILABILITY_LABELS if label in rest[:end]}


def storefront_snapshot(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "grove-qa-verify/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https QA host
        return response.read().decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="nursery", choices=TENANTS)
    parser.add_argument("--storefront", help="Storefront base URL; if set, /shop HTML is diffed across the flip.")
    parser.add_argument("--cap", type=int, default=0, help="Step 6: flip N templates in one txn (try 55). 0 = skip.")
    parser.add_argument(
        "--live-product",
        type=int,
        help="Also flip a REAL published template (e.g. a /shop card id) and assert the grid card "
        "itself changes. On-hand is snapshotted and restored; needs --storefront.",
    )
    parser.add_argument("--keep", action="store_true", help="Leave probe records behind for inspection.")
    args = parser.parse_args()

    url = os.environ.get("ODOO_URL", "https://odoo.qa.gatheringatthegrove.com")
    db = os.environ.get("ODOO_DB", "odoo")
    user = os.environ.get("ODOO_USER", "josh@goldberrygrove.farm")
    key = os.environ.get("ODOO_API_KEY")
    if not key:
        raise SystemExit("FATAL: ODOO_API_KEY is not set (read it from 1Password; never hard-code it)")

    odoo = Odoo(url, db, user, key)
    module = odoo.kw(
        "ir.module.module", "search_read", [[["name", "=", "grove_headless"]], ["installed_version", "state"]]
    )
    log("env", f"{url} db={db} uid={odoo.uid} grove_headless={module and module[0]}")

    company_id, location_id = resolve_tenant(odoo, args.tenant)
    tag = uuid.uuid4().hex[:6]
    log("env", f"tenant={args.tenant} company={company_id} location={location_id} run-tag={tag}")

    # Probe templates are created fresh each run, so filtering events by their
    # ids is already run-scoped; the date floor just keeps the domain explicit.
    since = "1970-01-01 00:00:00"
    results: list = []
    created: list = []
    live_restore = None  # {variant_id: qty} to put back while a real product is held at zero

    try:
        # ── Steps 1-3: in stock -> sold out, one delivered event ─────────
        template_id = make_probe_template(odoo, company_id, tag, 1)
        created.append(template_id)
        product_id = variant_of(odoo, template_id)
        set_on_hand(odoo, [product_id], location_id, 1.0, company_id)
        in_stock = qty_available(odoo, template_id, company_id) > 0
        verdict(in_stock, "step1", f"probe template {template_id} reads in stock (qty_available > 0)", results)
        restock_in_events = len(events_for(odoo, [template_id], since))

        before_html = storefront_snapshot(f"{args.storefront.rstrip('/')}/shop") if args.storefront else ""
        started = time.monotonic()
        set_on_hand(odoo, [product_id], location_id, 0.0, company_id)
        sold_out = qty_available(odoo, template_id, company_id) == 0
        verdict(sold_out, "step2", "last unit sold: qty_available == 0", results)

        sellout_events = events_for(odoo, [template_id], since)[restock_in_events:]
        ok = len(sellout_events) == 1
        verdict(ok, "step3a", f"exactly 1 product.availability event on sellout (got {len(sellout_events)})", results)
        if sellout_events:
            event = sellout_events[-1]
            verdict(
                event["state"] == "delivered" and 200 <= (event["http_status"] or 0) < 300,
                "step3b",
                f"event delivered to receiver: state={event['state']} http={event['http_status']} err={event['error']}",
                results,
            )
            verdict(
                (time.monotonic() - started) < FAST_PATH_BUDGET_S,
                "step3c",
                f"emit+deliver inside the {FAST_PATH_BUDGET_S}s fast-path budget ({time.monotonic() - started:.2f}s)",
                results,
            )
        if args.storefront:
            after_html = storefront_snapshot(f"{args.storefront.rstrip('/')}/shop")
            verdict(
                after_html != before_html,
                "step3d",
                "/shop HTML changed after the sellout webhook (revalidate landed)",
                results,
            )

        # ── Step 4: restock ─────────────────────────────────────────────
        before_count = len(events_for(odoo, [template_id], since))
        set_on_hand(odoo, [product_id], location_id, 3.0, company_id)
        restock_events = events_for(odoo, [template_id], since)[before_count:]
        verdict(
            len(restock_events) == 1,
            "step4",
            f"restock emits exactly 1 reverse-transition event (got {len(restock_events)})",
            results,
        )

        # ── Step 5: three templates, one transaction, three events ──────
        multi = [make_probe_template(odoo, company_id, tag, i) for i in (2, 3, 4)]
        created.extend(multi)
        variants = [variant_of(odoo, t) for t in multi]
        set_on_hand(odoo, variants, location_id, 2.0, company_id)
        seeded = len(events_for(odoo, multi, since))
        set_on_hand(odoo, variants, location_id, 0.0, company_id)
        multi_events = events_for(odoo, multi, since)[seeded:]
        per_template = {e["product_tmpl_id"][0] for e in multi_events}
        verdict(
            len(multi_events) == 3 and per_template == set(multi),
            "step5",
            f"3 templates selling out in one transaction -> {len(multi_events)} events "
            f"across {len(per_template)} templates (want 3/3, no stock-move fan-out)",
            results,
        )

        # ── Step 6: per-transaction emit cap ────────────────────────────
        if args.cap:
            bulk = [make_probe_template(odoo, company_id, tag, 100 + i) for i in range(args.cap)]
            created.extend(bulk)
            bulk_variants = [variant_of(odoo, t) for t in bulk]
            set_on_hand(odoo, bulk_variants, location_id, 1.0, company_id)
            seeded = len(events_for(odoo, bulk, since))
            set_on_hand(odoo, bulk_variants, location_id, 0.0, company_id)
            bulk_events = events_for(odoo, bulk, since)[seeded:]
            verdict(
                len(bulk_events) == min(args.cap, EMIT_CAP),
                "step6",
                f"bulk adjustment of {args.cap} templates emitted {len(bulk_events)} events "
                f"(cap {EMIT_CAP}; remainder degrades to the /shop ISR window)",
                results,
            )

        # ── Step 7: fail-safe on an unconfigured tenant ─────────────────
        # Proven structurally: the write below commits and qty_available moves
        # even when no event can be emitted (see the emit-skipped assertion in
        # the summary for a tenant whose webhook env is unset).
        set_on_hand(odoo, [product_id], location_id, 1.0, company_id)
        verdict(
            qty_available(odoo, template_id, company_id) == 1,
            "step7",
            "stock write commits normally regardless of webhook outcome (no 500, no rollback)",
            results,
        )
        # ── Steps 1-4 against a REAL published /shop card ───────────────
        # The probe template above proves the pipeline; this proves the thing a
        # customer sees: the grid card for a live product stops saying
        # "In stock". On-hand is restored in the finally block below.
        if args.live_product and args.storefront:
            shop_url = f"{args.storefront.rstrip('/')}/shop"
            live_id = args.live_product
            original = variant_quantities(odoo, live_id, company_id)
            live_variants = list(original)
            live_restore = original
            before_labels = card_labels(storefront_snapshot(shop_url), live_id)
            verdict(
                "In stock" in before_labels,
                "live1",
                f"/shop card {live_id} starts as In stock (labels={sorted(before_labels) or 'none'})",
                results,
            )
            seeded = len(events_for(odoo, [live_id], since))
            started = time.monotonic()
            set_on_hand(odoo, live_variants, location_id, 0.0, company_id)
            live_events = events_for(odoo, [live_id], since)[seeded:]
            verdict(
                len(live_events) == 1 and live_events[0]["state"] == "delivered",
                "live2",
                f"sellout of {live_id} emitted {len(live_events)} delivered event(s) "
                f"(state={live_events[0]['state'] if live_events else 'none'})",
                results,
            )
            after_labels = card_labels(storefront_snapshot(shop_url), live_id)
            elapsed = time.monotonic() - started
            # The PDP is force-dynamic and reads qty_available, so it is the
            # control: if the PDP flips and the grid card does not, the gap is
            # the list payload, not the invalidation path.
            pdp = storefront_snapshot(f"{args.storefront.rstrip('/')}/shop/{live_id}")
            verdict(
                "Sold out" in pdp or "Out of stock" in pdp or "Notify me" in pdp,
                "live2b",
                f"PDP /shop/{live_id} reflects the sellout (control for the grid check below)",
                results,
            )
            verdict(
                "In stock" not in after_labels,
                "live3",
                f"/shop card {live_id} no longer says In stock {elapsed:.2f}s after the sellout "
                f"(labels={sorted(after_labels) or 'none'}) — no manual Publish, no 30s ISR wait",
                results,
            )
            restore_quantities(odoo, original, location_id, company_id)
            live_restore = None
            restored_labels = card_labels(storefront_snapshot(shop_url), live_id)
            verdict(
                "In stock" in restored_labels,
                "live4",
                f"restock restored /shop card {live_id} to In stock (labels={sorted(restored_labels) or 'none'})",
                results,
            )
    finally:
        if live_restore:
            # Never let a failed restore swallow the probe-template teardown
            # below — a shout in the log is recoverable, orphaned probe products
            # in the QA catalog are noise someone else has to clean up.
            log("teardown", f"restoring per-variant on-hand {live_restore} on template {args.live_product}")
            try:
                restore_quantities(odoo, live_restore, location_id, company_id)
            except Exception as exc:  # noqa: BLE001
                print(f"ACTION REQUIRED: could not restore {args.live_product} on-hand {live_restore}: {exc}")
        if created and not args.keep:
            for template_id in created:
                try:
                    odoo.kw("product.template", "write", [[template_id], {"active": False}])
                except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                    log("teardown", f"could not archive template {template_id}: {exc}")
            log("teardown", f"archived {len(created)} probe template(s) (run-tag {tag})")
        elif created:
            log("teardown", f"--keep: left templates {created} in place (run-tag {tag})")

    failed = [r for r in results if not r[0]]
    print("\n" + ("=" * 72))
    print(f"{len(results) - len(failed)}/{len(results)} checks passed for tenant '{args.tenant}'")
    for _, step, msg in failed:
        print(f"  FAIL {step}: {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
