# Storefront publish webhook contract (Odoo → grove-sites)

GOL-985 · "PR B" of the guides chain. This is the wire contract the Odoo
publisher (`grove_headless/models/grove_publish.py`) and the grove-sites
receiver **must implement identically**. It supersedes the shared-secret
`x-grove-revalidate-secret` scheme on `apps/hub/app/api/revalidate/route.ts`
with a per-tenant HMAC signature.

## Trigger

An operator approves a species guide on the Odoo product form
(`grove_guide_ready = true`, PR A / GATH-130) and clicks **Publish Guide to
Storefront**. Odoo signs a small JSON event and POSTs it to the tenant's
grove-sites webhook so Next.js revalidates the affected page(s). Every attempt
is recorded in the `grove.publish.event` ledger (audit + one-click replay).

## Request

```
POST <tenant webhook URL>
Content-Type: application/json
X-Grove-Event:         guide.publish
X-Grove-Delivery:      <opaque id, unique per logical publish>
X-Grove-Tenant:        goldberry | ggg | nursery
X-Grove-Signature-256: sha256=<hex HMAC-SHA256(secret, RAW request body)>
```

Body (example):

```json
{
  "event": "guide.publish",
  "delivery_id": "6f1c…",
  "occurred_at": "2026-07-30T19:15:55Z",
  "tenant": "goldberry",
  "kind": "product",
  "product": {
    "id": 42,
    "template_id": 42,
    "slug": "american-persimmon",
    "name": "American Persimmon"
  },
  "guide_ready": true
}
```

Odoo serializes with **sorted keys, no whitespace** and sends those exact bytes
(`data=`, never re-encoded). The body is deterministic, so a replay of the same
`delivery_id` produces byte-identical output.

## Signature verification (receiver side)

1. Read the **raw** request body as bytes/text — do **not** `JSON.parse` then
   re-stringify; that changes the bytes and breaks the MAC.
2. Compute `sha256=` + hex `HMAC-SHA256(GROVE_PUBLISH_WEBHOOK_SECRET, rawBody)`.
3. Compare to `X-Grove-Signature-256` in constant time
   (`crypto.timingSafeEqual`). Reject with **401** on mismatch/missing.

Node reference:

```ts
import { createHmac, timingSafeEqual } from "node:crypto";

function verify(rawBody: string, header: string | null, secret: string): boolean {
  if (!header || !secret) return false;
  const expected = "sha256=" + createHmac("sha256", secret).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(header);
  return a.length === b.length && timingSafeEqual(a, b);
}
```

## Secrets (per tenant)

The signing secret is shared out-of-band between Odoo and grove-sites — one per
tenant. Odoo reads it from the environment; grove-sites reads the matching value
(the "per-tenant key wiring" deferred by checkout-flip PR #284):

| Tenant     | Odoo env (server)                        | grove-sites env        |
| ---------- | ---------------------------------------- | ---------------------- |
| goldberry  | `GROVE_PUBLISH_WEBHOOK_SECRET_GOLDBERRY` | `GROVE_PUBLISH_WEBHOOK_SECRET` |
| ggg        | `GROVE_PUBLISH_WEBHOOK_SECRET_GGG`       | `GROVE_PUBLISH_WEBHOOK_SECRET` |
| nursery    | `GROVE_PUBLISH_WEBHOOK_SECRET_NURSERY`   | `GROVE_PUBLISH_WEBHOOK_SECRET` |

The destination URL is likewise per tenant on the Odoo side
(`GROVE_PUBLISH_WEBHOOK_URL_<TENANT>`), each pointing at that tenant's
grove-sites deployment. Both fall back to the unsuffixed
`GROVE_PUBLISH_WEBHOOK_URL` / `GROVE_PUBLISH_WEBHOOK_SECRET` for single-tenant
setups. Provisioning of the actual secret values is DevOps (Terra).

## Receiver responsibilities

- **Verify the signature first**; 401 on failure.
- **Dedupe on `X-Grove-Delivery`** (retries reuse it) — revalidation is
  idempotent, so at-least-once delivery is fine; just don't error on a repeat.
- **Revalidate** the guide's page(s) from `product.slug` for the given
  `tenant`, then the product's parent listing. Return `200` with a small JSON
  ack (`{ "ok": true }`); any non-2xx marks the delivery `failed` in Odoo and
  makes it retryable.

## Responses Odoo acts on

- `2xx` → event `delivered`.
- any other status → event `failed`, body stored (truncated) for debugging,
  retryable from the Publish Events log.
- transport error (timeout / connection refused) → event `failed`, retryable.
