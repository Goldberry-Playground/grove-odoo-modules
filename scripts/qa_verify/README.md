# QA verification scripts

One command per release-train QA item, run against a live environment, printing
`PASS`/`FAIL` per acceptance criterion with the evidence inline. They exist so a
train window costs minutes of someone's evening instead of an afternoon of
clicking, and so the same checks are repeatable at the next train.

Credentials always come from the environment — never a literal in these files.

## availability_invalidation.py — GOL-1896 / GOL-2337

Stock-change storefront invalidation: a sellout must reach the storefront over
the `product.availability` webhook (fast path, ~1s) rather than waiting out the
`/shop` ISR window (30s safety net).

```bash
export ODOO_API_KEY="$(op read 'op://Grove QA/Gather At the Grove QA Odoo/odoo_mcp_qa_api_key')"
python3 scripts/qa_verify/availability_invalidation.py \
    --tenant nursery \
    --storefront https://nursery.qa.gatheringatthegrove.com \
    --live-product 153        # optional: also flip a REAL /shop card
    # --cap 55                # optional: step 6, the per-transaction emit cap
```

What it drives, mapped to the GOL-2337 manual script:

| Check | GOL-2337 step | Asserts |
| --- | --- | --- |
| `step1`/`step2` | 1-2 | probe template goes in stock, then sells out |
| `step3a-c` | 3 | exactly ONE delivered `product.availability` event, inside a 5s budget |
| `step3d` | 3 | `/shop` HTML changed after the delivery |
| `step4` | 4 | restock emits the reverse transition |
| `step5` | 5 | 3 templates crossing in ONE transaction → exactly 3 events (no stock-move fan-out) |
| `step6` | 6 | `--cap N` templates in one transaction → at most `_AVAILABILITY_EMIT_CAP` (50) emits |
| `step7` | 7 | the stock write commits regardless of webhook outcome (no 500, no rollback) |
| `live1-4` | 1-4 | a REAL published `/shop` card, with the PDP as the stock-aware control |

The probe creates its own `ZZ QA availability probe …` templates and archives
them on the way out (`--keep` to inspect a failure). `--live-product` mutates a
real product's on-hand and restores the **per-variant** split it found.

### Footguns learned the hard way

- **Use JSON-RPC, not XML-RPC.** `stock.quant.action_apply_inventory` returns
  `None`; Odoo's XML-RPC marshaller refuses to encode it ("cannot marshal None"),
  so the write lands and the response still blows up.
- **Restore per variant, never per template.** `product.template.qty_available`
  is the SUM over variants. Writing that total back onto each variant doubles
  real stock (Dogwood 10 = potted 10 + bareroot 0 came back as 10 + 10).
- **Never measure a storefront right after an Odoo restart.** `/shop` catches a
  failed catalog fetch and renders `mockProducts` (ids 201-208, every card
  hardcoded "In stock"), and that render gets cached. Confirm the page shows real
  catalog names before trusting any availability reading.
- **The emit needs `GROVE_PUBLISH_WEBHOOK_URL_<TENANT>` + `_SECRET_<TENANT>` in
  the Odoo process env.** Missing config raises inside `_emit` *before* the
  ledger row is written, so an unconfigured tenant produces zero
  `grove.publish.event` rows — indistinguishable from "no transition happened"
  unless you know to check. The receiver side needs the byte-identical secret in
  the tenant app's `GROVE_PUBLISH_WEBHOOK_SECRET` or every delivery 401s.
