# Order lanes, holds and spring zone batches (design)

**Status:** for Josh's approval, 2026-11-08. Reviewed by a builder check. Nothing built. Repos: `grove-odoo-modules` (`grove_headless`) and `grove-sites` (`apps/discord-bridge`).

**First hard date:** zones 8 to 10 start shipping Mar 1, so their 5-day warning fires Feb 24, 2027. Spring batching and warnings must be live before then.

## Why

- Promises to customers are spoken and forgotten. Josh: "we just tell each other and forget." A customer asked for his chestnuts to wait until dormant, and nothing recorded it.
- Customers pick fall when they really mean spring, and there is no way to move the order.
- The batch system is clunky for one order, and Wesley has to log in to Odoo for everything.
- Labels bought outside Odoo drift: Pirate Ship refunds and reprints never reach it.

## Decisions (Josh, 2026-11-08, do not re-open)

1. Every order goes down one of two lanes. **Spring orders are batched by zone** in a separate lane. **Fall preorders and everything else are fulfilled as they come in.**
2. Daily order work happens in the **grove-orders** Discord channel, without logging in to Odoo.
3. **Mark shipped asks for the tracking code and refuses to go through without one.**
4. An order held for a later date is recorded on the order, with alerts, so it can't be forgotten.
5. A fall order that is really a spring order can be **moved into the spring batch system**.
6. Each spring zone gets a **5-day warning and a day-of warning** that Wesley needs to process it.
7. Individual orders use typed tracking, not Pirate Ship export and import. The file export and import stay for spring batches.
8. Wesley can build a zone's batch and buy labels **from the 5-day warning**, knowing customers are charged at import. Boxes go out on or after the zone's start date.
9. Discord gets a **Label refunded** button.
10. **No automatic customer email** when an order moves between fall and spring. Staff tell the customer, and later emails use the new season.

## Goals

- From an order card, Wesley can hold, move or ship an order in seconds.
- No box is marked shipped without a tracking number.
- A held order or an opening spring zone can't pass without someone answering.
- A batch run covers one spring zone only, and never catches individual-lane orders.

## Non-goals

- **"Pre-order until date X" on the storefront.** Separate design, next. Includes showing the customer the date and making the fall or spring choice clearer.
- No Pirate Ship API. Labels are still bought by hand.
- A hold never ships or charges anything on its own.
- No change to how balances are calculated, or to the existing #sales, #support and #shipping alerts.
- USPS tracking stays muted.

## Anti-goals (a failure even if it works)

- A box leaves with no tracking recorded.
- A Discord click charges a customer twice, or charges before the order really ships.
- Alerts become noise and people stop reading them.

## What exists today

- **Bridge:** `apps/discord-bridge` already bot-posts the order card to a per-brand channel (`DISCORD_ORDER_CHANNELS`, keyed on `company_id`), handles button clicks (`ord:shipped:<orderId>`), opens modals, edits cards, and limits order actions to `DISCORD_ORDER_OPERATOR_IDS`. It calls Odoo with a scoped bearer key.
- **Odoo endpoint:** `POST /grove/api/v1/orders/<id>/mark-shipped`, body only `{actor}`. It runs `_operator_mark_shipped` (settles, emails).
- **Order data:** `grove_ship_wave` (fall or spring, set at checkout, readonly) and `grove_usda_zone` (stored, computed from the shipping ZIP).
- **Schedule:** `preorder_waves` and `merge_calendar_override` give each zone's window, including admin overrides. Dormancy window is `shipping_boxes.dormancy_window(env)`.
- **Label entry:** `action_grove_record_hand_label` (awaiting label or assigned to wave only) and `action_grove_replace_label` (label purchased or shipped).
- **Batches:** `_eligible_orders` takes all awaiting-label and wave-assigned orders. `_open_batch` allows one open batch per company, used by `build_open_batch`, `import_pirateship_file` and the runner endpoint.

## Design

### 1. Lanes

An order is in the **spring lane** when `grove_ship_wave == "spring"` and it is a ship order. Every other ship order is in the **individual lane**. Pickup and Printify orders are in neither. The card shows the lane, and spring cards show the zone (**Spring · Zone 6**).

### 2. Mark shipped with tracking (individual lane)

The **Mark shipped** button opens a modal with **Tracking number** and **Label cost**, both required. Both are prefilled if a label is already on the order.

The endpoint takes `{actor, tracking, cost}` and refuses a missing or invalid tracking number or cost with a message shown only to the clicker. On success, in one request:

1. If the order is `deposit_paid` with a fall wave, assign it to its wave.
2. Record the label, or replace it if the number differs.
3. Run `_operator_mark_shipped`, so the balance uses the real label cost and the email carries tracking.

The bridge edits the card to show carrier, tracking, cost and who marked it.

**Odoo's own Mark shipped** (server action) refuses orders with no tracking too, so there is no back door. The Odoo endpoint also refuses a held order, and a spring-lane order with no batch label.

**Several boxes:** the typed path holds one number, and the other numbers go in a note. Spring batches record every box.

### 3. Holds

New on `sale.order`: `grove_hold_until` (date), `grove_hold_reason` (text), `grove_hold_set_by`, `grove_hold_last_nudge`.

- **Hold** button opens a modal: **Hold until** (typed, for example "Oct 15") and **Why** (required). The date is suggested as the later of the dormancy start and the start of the order's zone window, and can be changed. Dates in the past are refused.
- The card shows **ON HOLD until Oct 15**, the reason and who set it, with **Change date** and **Release hold**. Mark shipped is hidden while held.
- A held order is excluded from batch building.
- **Nudge:** a daily cron at 8am Eastern posts to grove-orders for each hold that has come due, with **Ready to ship**, **Snooze a week** and **Change date**. It repeats daily until answered.
- Every change writes to the order's chatter. Odoo gets the same actions and an "On hold" filter, for when someone is already there.

### 4. Move between fall and spring

**Move to spring** and **Move to fall** buttons change `grove_ship_wave`, log to chatter and update the card. They refuse if the order already has tracking (use **Label refunded** first), or if it has shipped, been cancelled, or has no known zone. Deposits and payments don't change. There's no other date limit: an order moved after a window has closed waits for that season's next window.

### 4a. Label refunded

A **Label refunded** button on a card with a label, for orders at *Label purchased* only. It writes the old number to chatter, clears the tracking, carrier and label fields, and walks the stage back to needing a label: *Assigned to ship wave* for a wave order, otherwise *Paid, awaiting label*. This needs one new backward transition in `_GROVE_TRANSITIONS`.

It refuses once an order has shipped, which stays a job for Josh. Money doesn't move: if a batch import already charged the balance, it stays charged and settlement stays a no-op next time.

### 5. Spring batches by zone

`grove.label.batch` gains `zone`. One open batch per company **and zone**. `_eligible_orders` for a zone batch takes only spring-lane orders in that zone that aren't held. Export, buy and import stay in Odoo, once per zone.

**When a batch can be built:** from 5 days before the zone's `ship_start` through its `ship_end`. Spring orders sit at *Deposit paid* and can't be packed from there, so building assigns each one to its wave first. The window check in `_wave_hold_reason` opens 5 days early for batch building only.

**After import:** balances are charged at import, as the batch path does today (GOL-2901). Grove-orders gets "Zone 6: 14 labels recorded" with **Mark all shipped**, which covers the orders not yet marked, works only from `ship_start`, and sends the shipped emails. Each spring card also keeps its own **Mark shipped**, prefilled, so a batch packed over several days or one box held back still works.

Building again after a zone's batch is purchased picks up late orders in a new batch.

`_open_batch` callers need updating: `import_pirateship_file` has to match rows across every open batch, and the runner endpoint needs a zone. Prod has no open batches, so no data migration is needed.

### 6. Spring zone warnings

A daily cron at 8am Eastern checks each zone's spring `ship_start`, using the merged calendar:

- **5 days before:** "Zone 6 spring shipping starts in 5 days: 14 orders, 3 on hold."
- **Day of:** "Zone 6 spring shipping starts today: 14 orders ready to batch."

Only zones with orders post. Each alert sends once per zone and year, recorded in a small new model. If the cron missed a day, it catches up on the next run. Spring `order_by` is 7 days before `ship_start`, so the 5-day count is close to final. Moved orders can still change it.

**Late orders:** from the day-of alert through `ship_end`, a daily post lists any spring orders in that zone not yet in a batch: "Zone 6: 2 spring orders still need a batch." A released hold or a moved order lands here, so it can't sit silently. The same post names orders in a built batch whose labels were never imported ("batch built, labels not imported") and orders labelled but not marked shipped ("14 labelled, press Mark all").

### 7. Orders open at launch

New cards apply to new orders. At launch, repost the orders still open once, so they get the new buttons. On 2026-11-08 that's 5 ship orders, none of them spring, so it isn't a flood.

## Calls I made (Josh to check)

- Nudges repeat daily until answered.
- Spring batch export and import stay in Odoo for this version, one login per zone.
- Existing #shipping alerts stay where they are. Only the new alerts use grove-orders.
- Everyone in `DISCORD_ORDER_OPERATOR_IDS` can use all the new buttons.
- Mark all shipped waits for the zone's start date, and individual spring cards keep their own button.
- Building a batch assigns its spring orders to their wave automatically.
- A daily late-orders post runs through the end of each zone's window.
- Label refunded works only before an order has shipped.
- Odoo's Mark shipped refuses without tracking, matching Discord.

## Left to the builder

Field and model names, the modal date parser, how the card is rebuilt after each action, cron wiring, test layout, and which release train each phase rides.
