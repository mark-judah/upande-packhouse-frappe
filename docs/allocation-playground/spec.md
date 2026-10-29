# Allocation Playground: spec

Status: draft for review. Replaces the confirmation step of Allocation Planning v2
(`www/sales-allocation-planning-v2.html`, `api/v2/allocation_planning.py`).

## 1. Why (what the last 3 weeks of kaitet-group showed)

Orders due 7-27 Oct 2026, read-only analysis. Upper bounds, because the shelf count includes ghost buckets.

| Finding | Measured |
|---|---|
| Unfilled stems where the shelf covered the pool's total demand (variety x length x day, 10am D-1) | 1.02M of 2.52M |
| Real shortage in worked orders | 149k stems; 78k fillable by a longer same variety, 77k by an approved alternative |
| Avoidable discards (exact variety, or approved substitute) | about 253k of 1.08M (conservative) |
| Downgrades that missed a buyer for the longer length | 37k stems (42% of 88k downgraded) |
| Mixed groups left half-allocated | 37 (17 box, 20 bunch) |
| Lines with any confirmation today | 31 of 8,243 (0.4%), all Kapkolia |
| Revised forecast error, total / per variety-week | about 1% / about 30% |

(The 6-day discard age seen in the discards is the live `discard_age` setting, not a rule of this spec.)

Root causes: allocation is first-come on the live shelf, at 8-11am the day before delivery; the unit of work
is the line but the loss happens at the pool (variety x length x delivery day); confirmations are promises
that never reduce what another customer's allocator sees; nobody owns the balance of the whole book.

## 2. Goals and non-goals

Goals
1. Let sales plan the whole delivery day before allocation: confirm lines against real shelf stock or against forecast stock.
2. A confirmation against real stock reserves the buckets and stems, so allocation to another customer cannot take them.
3. Warn, with a concrete alternative, before the plan causes a premature downgrade, a squeezed strict customer, a broken mix or a discard.
4. Allocation releases the reservation it consumes. Reservations are cheap for allocation to query by bucket.
5. The cleanest page in the dashboard: a few numbers, one list, one action.

Non-goals (this spec)
- Blocking or warning allocators when they deviate from the plan (phase 3).
- A ghost-rate or stock-trust model (later; reservations are built so it can be added).
- Offline-order tagging, reservation override rules, ghost-bucket replacement rules (skipped for now, see 12).

## 3. Core concepts

- **Pool**: variety x length x delivery date x stock farm. The unit the page works in. Lines are the detail.
- **Real stock**: buckets on a shelf, allocatable per `core/stock.py` (`allocatable`).
- **Virtual stock**: forecast stems for the pool (section 6). Never tied to buckets.
- **Confirmation**: a planner's decision to cover stems of a sales-order line from a pool.
  - Source **Real**: reserves specific buckets and the stems taken from each.
  - Source **Virtual**: reserves stems only, against the pool's virtual balance.
- **Reservation**: the lock a Real confirmation puts on buckets. Allocation to a different customer cannot use them.
- **Conversion**: when real buckets arrive for a pool, Virtual confirmations are converted to Real (section 7).

### 3.1 Stock age states (no hard-coded days anywhere)

Every threshold is read from Production Settings through one resolver (`core/stock._config()`, the same one the
allocator and Stock Visibility use). The spec and the code never contain a number of days.

| Setting (Production Settings) | Live value (kaitet-group, 27 Oct) | Meaning in the plan |
|---|---|---|
| `amber_time` (days) | 0, which the allocator treats as 3 | from this age, a downgrade of the bucket needs no approval; the board marks the stock **Amber, use first** |
| `Shelf Locations.max_allocation_age` (per farm) | 5 (Karen 6) | last age at which a bucket can still be allocated |
| `discard_age` (days) | 6 | at this age the bucket is **Expired**: not allocatable, a discard candidate |
| `max_shelving_age` (days) | 6 | shelving refuses older buckets; used to cap how old arriving stock can be |
| `Shelf Locations.cooling_hours` (per farm) | 0 | newly shelved stock is not available until this many hours |

States for a bucket of age *a* days: **Fresh** (a below `amber_time`), **Amber** (a at or above `amber_time`, still allocatable),
**Expiring** (the last allocatable day: `a` equals the farm's `max_allocation_age`, or `discard_age` minus 1 if lower), **Expired** (not allocatable).
The board, the downgrade guard and the rescue radar use these states; none uses a literal.

Two things to fix when we build, both in the resolver:
1. `amber_time` of 0 is silently replaced by 3 (`ps.amber_time or 3`), and `discard_age` falls back to 5 where the setting says 6.
   The resolver will return the effective value, and the board will show it ("Amber from day 3"), so planners are never surprised.
   Recommendation: set `amber_time` explicitly in Production Settings rather than relying on the fallback.
2. Per-farm values come from `Shelf Locations`; a farm without a row uses no plan.

## 4. Data model

All new doctypes are non-submittable, module "Upande Packhouse".

### 4.1 Allocation Plan (header, one per delivery date and stock farm)
| Field | Type | Notes |
|---|---|---|
| delivery_date | Date | required |
| farm | Link Farm | stock farm; unique with delivery_date |
| status | Select | Draft, Active, Locked, Closed |
| locked_by, locked_on | Link User, Datetime | Locked = no new confirmations, allocation proceeds |
| notes | Small Text | |

Naming: `AP-{delivery_date}-{farm}`. Created on first confirmation. Closed automatically the day after delivery.

### 4.2 Allocation Confirmation (the record; one per line per source)
| Field | Type | Notes |
|---|---|---|
| plan | Link Allocation Plan | indexed |
| sales_order, sales_order_item | Link, Data | indexed on sales_order_item |
| customer | Link | |
| ordered_variety, ordered_length | Link Item, Link Stem Length | from the line |
| variety, length | Link Item, Link Stem Length | what is confirmed; differs from ordered on substitution or downgrade |
| stems | Float | confirmed stems |
| source | Select | Real, Virtual |
| status | Select | Planned (virtual), Reserved (real), Partly Fulfilled, Fulfilled, Released, Broken |
| mix_key | Data | sales_order + mix_group or bunch_group, for mixed lines; group is confirmed and released as a whole |
| flag | Select | None, Downgrade, Substitution, Both |
| guard_override_reason | Select | required when a red guard was overridden; picklist, see 9 |
| forecast_basis | Data | for Virtual: forecast week and basis used, for audit |
| fulfilled_stems | Float | filled by allocation |
| released_on, released_by, release_reason | Datetime, User, Select | Allocated, Order cancelled, Line changed, Delivery passed, Manual, Converted |

One live confirmation per (line, source). Changing stems edits it; the history is the Version log.

### 4.3 Bucket Reservation (one row per bucket per Real confirmation) -- the table allocation queries
| Field | Type | Notes |
|---|---|---|
| bucket_id | Link Bucket QR Code | **indexed** |
| confirmation | Link Allocation Confirmation | |
| sales_order_item | Data | **indexed**; who the bucket is reserved for |
| plan | Link | |
| stems | Float | stems of this bucket reserved (<= the bucket's free stems when reserved) |
| status | Select | Active, Consumed, Released, Broken |
| released_on | Datetime | |

Composite index (`status`, `bucket_id`). This is the only table the allocator needs:
`SELECT bucket_id, sales_order_item, SUM(stems) FROM tabBucket Reservation WHERE status = 'Active' AND bucket_id IN (...) GROUP BY bucket_id, sales_order_item`.

### 4.4 Compatibility with `Confirmed Stems`
The allocator today reads `Sales Order > custom_confirmed_stems_table` (farm, stems, sales_order_item) and
shows only confirmed lines at a farm, with pending = confirmed. Phase 1 dual-writes: every Reserved confirmation
also writes its Confirmed Stems row, so the allocator keeps working unchanged. The row holds everything confirmed for the line at that farm,
Real and Virtual together (`allocation_plan.refresh_mirror`): the allocator shows a farm only the lines confirmed there and caps what it offers
at that number, so stems still to arrive have to be in it.

## 5. Reservations and allocation (the link, built in phase 1)

Single choke point: extend `upande_packhouse.availability` next to `reserved_bucket_ids()` (discard holds, which both
the allocator and `core/stock.py` already use) with:

- `reserved_by_bucket(exclude_soi=None)` -> `{bucket_id: reserved_stems}` for Active reservations, excluding those
  held for the given line.

Effects:
1. `core/stock.py` treats reserved stems as a fourth split: shelved = held + allocated + **reserved** + free.
   `allocatable` excludes reserved stems for everyone except the line they are reserved for.
2. The allocator (`sales_allocation.py`), when allocating line X, calls `reserved_by_bucket(exclude_soi=X)`
   and skips or reduces those buckets. For line X it prefers its own reserved buckets first.
3. **Release by allocation.** After the allocator writes Bucket Allocations for line X from a reserved bucket,
   it marks that reservation row Consumed and adds to the confirmation's `fulfilled_stems`. When the line is fully
   allocated, or its OPL is submitted, every remaining Active reservation of that line is Released (reason: Allocated)
   and the confirmation becomes Fulfilled.
4. Other release triggers (doc events and a scheduler job): Sales Order cancelled or amended, line quantity or
   variety changed, delivery date passed, manual unconfirm, conversion of a Virtual confirmation.
5. **Broken**: if a reserved bucket disappears (marked not found, discarded, or removed from the shelf), its row
   becomes Broken, the confirmation drops to "Needs cover", and the board shows it. No automatic replacement yet.

Reserving buckets (the Real confirm): pick buckets oldest first (so aging stock is used first and discards fall
naturally), whole buckets until the stems are met; the last bucket may be partly reserved. Reserve inside one
transaction with a row lock on the Bucket Allocation Status rows to avoid two planners taking the same bucket.

## 6. Virtual stock

Per pool: `virtual = expected_arrivals(variety, length, delivery_date)` where
- arrivals are the **revised forecast** for the harvest day(s) that supply the delivery date, offset by
  `allocation_harvest_lead_days` (new Production Settings field, see 6.1), taken from Production Forecast Week rows
  (`revised_forecast_stems`, by length rows, normalised so the lengths sum to the revised total);
  arrivals older than `max_shelving_age` at delivery are not counted;
- variety-level forecasts are about 30% off per week, so each pool shows an **expected** and a **safe** figure
  (safe = expected x (1 - the variety's average forecast error this year; see 11.1);
- Virtual confirmations count against *expected*, and the board warns when confirmations pass *safe*;
- real stock already counted for the pool is not counted again.

Pool balance on the board: `real free + virtual expected - confirmed (real + virtual)`.

### 6.1 New Production Settings fields (new tab "Allocation Planning")

Everything the plan needs that is not already a setting. Defaults are seeded once on install, then owned by the business.

Labels are plain, with a one-line help text under each, so the people who maintain the settings understand them without this spec.

| Field | Label shown | Help text shown | Type | Seed |
|---|---|---|---|---|
| `allocation_harvest_lead_days` | Days from cutting to delivery | How many days before delivery the flowers are cut. Used to work out what the farm will supply. | Int | 1 |
| `allocation_horizon_days` | Days ahead people can plan | How many delivery days ahead the planning page lets you plan. | Int | 3 |
| `allocation_conversion_minutes` | Check for new stock every (minutes) | How often expected stock is matched to flowers that reach the shelf. | Int | 15 |
| `allocation_use_first_alert_stems` | Tell me when old stock is at least (stems) | Smallest amount of getting-old stock that is worth a "use first" suggestion. | Int | 500 |

Existing fields reused as is: `amber_time`, `discard_age`, `max_shelving_age`, `Shelf Locations.max_allocation_age`,
`Shelf Locations.cooling_hours`, `transfer_hub_farm`, `allow_*` and `show_unscheduled_*`.

## 7. Conversion (virtual to real)

On each shelving event and on a job every `allocation_conversion_minutes`: for every pool with Virtual confirmations and new free real buckets,
convert oldest-plan-first: create Bucket Reservation rows, set source Real, status Reserved, release reason
Converted on the virtual part. The planner sees a toast, not a task. Shortfalls stay Virtual.

## 8. Guards (computed on every confirm; warn, never silently block)

Each guard returns severity (green / amber / red), a one-line message, and concrete actions.

| Guard | Fires when | Suggestion shown |
|---|---|---|
| Downgrade | confirmed length > ordered length | green if the stock is **Amber or Expiring** (age states, 3.1) or no line wants the longer length; amber if another line at that length is uncovered (names the customers); red if a strict-spec line at that length is uncovered |
| Variety squeeze | confirming variety V to a line whose spec approves alternatives, while a single-approved-variety line for V is uncovered | swap the flexible line to the approved alternative with cover |
| Mix | a mixed box or bunch group is confirmed with any member variety uncovered | the bottleneck variety and an approved substitute; group is confirmed as a whole or not at all |
| Discard risk | Fresh stock is chosen while **Amber or Expiring** buckets of the same variety and length (or approved substitute) total at least `allocation_use_first_alert_stems` | use the aged buckets first (default for reservation) |
| Over-promise | confirmed (Virtual) passes safe, or Real stems exceed free stems | reduce, or confirm the rest as Virtual |

Rules draw on Specifications (`Spec Approved Variety`, `Spec Box Item`) and lines' `custom_mix_group`,
`custom_bunch_group`, `custom_mixed_box`, `custom_mixed_bunch`. Lines with no spec are treated as strict.
Age rules come from Production Settings (3.1); the guards compare ages with the resolved settings and never with literals.

Going ahead: the planner can always confirm anyway; no reason is asked for.

## 9. UI

Principle: **one list, one action, numbers only when they change a decision.** Three colours only (green, amber, red),
one typeface weight scale, no nested tables on the first screen.

### 9.0 Plain words only

The page uses everyday words. Terms from this spec (pool, virtual, guard, haircut, horizon, D-3, conversion, scenario) never appear
on screen, in messages, or in Production Settings labels. Dates are written as people say them ("Tomorrow", "Wed 28 Oct"),
never as "D-1" or "D-3".

| Internal term | What the user sees |
|---|---|
| Pool | **Variety and length** (for example "Moonwalk 62cm") |
| Real stock | **On the shelf** |
| Virtual stock | **Expected from the farm** |
| Expected / safe figure | **Expected from the farms** (shown by farm, never by greenhouse) / **Safe to promise** |
| Reservation, Reserved | **Held for [customer]** |
| Confirmation, confirm | **Confirm** (button: "Confirm") |
| Broken reservation | **Bucket missing**, "Needs stock" |
| Guard | **Heads-up** |
| Over-promise | **More than we have** |
| Variety squeeze | **Another customer needs this variety** |
| Discard risk | **Getting old: use first** |
| Amber stock | **Getting old** (shows "day 3") |
| Expiring / Expired | **Last day to use** / **Too old** |
| Downgrade | **Longer than ordered** |
| Mixed group | **Mixed box** / **Mixed bunch** |
| Conversion | happens quietly; shown as "Now on the shelf" |
| Scenario mode | **Try it out** (button "Keep changes" / "Discard") |
| Rescue radar | **Use first** list |
| Haircut | not shown; the page shows **Expected from the farms** and **Safe to promise**, with one line saying why they differ |
| Lock / Locked | **Done for the day** |

Heads-up messages are one plain sentence and say what to do, for example:
"If you send 72cm here, 2 customers who need 72cm will be short by 1,200 stems. Send 62cm that is getting old instead?"
Numbers are shown as plain stems (with thousands separators), buckets, and customers, never as ratios or percentages unless the user asked for one.

### 9.1 Layout
```
 Plan   Wed 28 Oct   [ Kapkolia v ]                       Needs attention | All
 ------------------------------------------------------------------------
  Lines confirmed   Stems left to confirm   Short   Use first
     82%               148k             12k          9k
 ------------------------------------------------------------------------
  VARIETIES (most urgent first)           |  Moonwalk 62cm
  ● Moonwalk 62   [####==..]  -3k         |  On the shelf 18k + expected 6k
  ● Aqua 52       [######=.]  +2k         |  Orders 27k            [ Confirm all ]
  ○ Madam Red 42  [########]  ok          |  ---------------------------------
  ...                                      |  FLOWERS EU      6,000   [ 6,000 ]
                                           |  FASHION GARDEN  4,200   [      ]
                                           |  Mix 1 (6 varieties)     [ Confirm box ]
```
- **KPI strip: four numbers.** Lines confirmed %, stems left to confirm, stems short, aging stems to use first.
- **Variety list (left):** one row per variety and length, a single bar split into Held | Confirmed | On the shelf | Short, and a status dot.
  Default tab "Needs attention" shows only red and amber rows; "All" is one click away.
- **Detail (right):** one header line (on the shelf, expected from the farm, ordered), then customer lines with one input each. Everything
  else (bucket list, age, shelves) is behind a "Details" link in a drawer.
- **Mixed boxes and bunches** are one card showing which variety is missing, not six rows.
- **Where the stock comes from** is automatic: stock on the shelf is used first, anything beyond comes from the expected harvest; the planner never picks.
- **Heads-up popup** when something needs attention: left, what happens in plain words; right, buttons such as `Use Pink Athena instead`,
  `Use 52cm instead (4,000 available)`, `Confirm anyway`. Small, harmless heads-ups do not interrupt;
  they show as a quiet tick on the row.
- **Try it out:** a toggle makes edits local (nothing is held) and shows a before and after of the four numbers;
  "Keep changes" saves them, "Discard" throws them away. This is the playground.
- No raw stock tables, charts or per-farm column grids on the first screen. Export and filters live behind one menu.
- Mobile/tablet: the list and detail stack; the Confirm button is a fixed bar.

### 9.2 Reuse
Built on the existing packhouse v2 shell (`PH.*` helpers, `ph-*` classes). This board IS the Allocation Planning page
(`/sales-allocation-planning-v2`); the old grid is replaced, and its old read endpoint (`api/v2/allocation_planning.py`) stays for now.

## 10. API (`api/v2/allocation_playground.py`, all `@frappe.whitelist()`)

| Method | Purpose |
|---|---|
| `get_board(delivery_date, farm, view)` | KPI strip and pool list |
| `get_pool(delivery_date, farm, variety, length)` | pool detail: lines, stock, virtual, current confirmations |
| `preview_confirmation(lines)` | runs the guards, returns severity, message and actions; writes nothing |
| `confirm(lines, override_reason=None)` | creates or updates confirmations, reserves buckets, dual-writes Confirmed Stems |
| `release(confirmation, reason)` | manual unconfirm |
| `suggest_swaps(delivery_date, farm)` | ranked swap and rescue suggestions |
| `convert_virtual(plan)` | the conversion step (also run by the job) |
| `apply_scenario(changes)` | commits a "Try changes" session |

Allocator-facing helpers live in `availability.py` (section 5), not in this module.

## 11. Phasing

- **Phase 1 (build now):** doctypes; board and pool UI; Real confirmation with bucket reservation; Virtual confirmation from the revised forecast; allocator reads reservations and releases them; Confirmed Stems dual-write; release triggers.
- **Phase 2:** guards, popup, swap and rescue suggestions, virtual-to-real conversion, scenario mode.
- **Phase 3:** plan-to-allocation guard rails (warn or block allocators who deviate), scorecards, ghost-rate trust.

### 11.1 Status

- **Phase 1:** built (doctypes, board, Real and Virtual confirmations, reservations read and released by allocation).
- **Phase 2:** built. Heads-ups (`api/v2/allocation_guards.py`) with the popup and a reason for red ones; Try it out; Use first;
  a longer length per line; a whole mixed box or bunch confirmed as one; expected stems turning into held buckets
  (`allocation_plan.convert_virtual`, on opening the board and on a job every few minutes).
- **Decision taken in phase 2: another variety is advice, not an action.** The allocator allocates the variety on the order line
  (no pick row in three weeks used a different one), so holding another variety's buckets for that line would tie stock up that
  allocation cannot use. The "Another customer needs this variety" heads-up names who is short and links to the order so sales can change
  the variety there. Making the allocator honour a planned substitute belongs to phase 3.
- **Changes after review of phase 2:** "Use a longer length" and "Confirm whole box" links removed. A longer length is now offered inside the
  heads-up that needs it ("Use 52cm instead (4,000 available)"); a mixed line shows one "Confirm this box" button instead of a stems box
  (with "Take it out" once confirmed). The not-enough message now says how many stems were asked, how many can be covered, where they come
  from (shelf and farms) and how many would be short, with a button for the part we are sure of. The detail panel shows ordered, on the shelf,
  expected from the farms (by farm, because the forecast is per greenhouse and a greenhouse belongs to a farm) and safe to promise, with a line
  explaining the margin held back.
- **Changes after review 2 (current behaviour):**
  - **The forecast margin is the forecast's own error.** The fixed percentage setting (and its Production Settings field) is gone. For each
    variety, "safe" = what is still to be cut x (1 - the variety's average error this year), where the error is the weighted average over
    finished weeks of |revised - actual| / actual (`allocation_playground.forecast_error`; actuals exist only for "all lengths" rows). The page
    says it in one line ("off by 24% on average this year ... so we count 76% of what is still to be cut"). With no finished week to judge by,
    none of it counts as safe.
  - **More stock types to confirm against, most certain first:** on the shelf; received, not shelved (buckets waiting for a shelf, within the
    discard age, with the cold room from the receiving entry's target warehouse; held bucket by bucket like shelf stock);
    harvested, not received yet (the cutting day's production on its way); still to be cut (revised forecast for the cutting day less what was
    harvested that day, shown by farm). Confirmations the shelf and received stock cannot cover are confirmed from the rest and held when the
    flowers arrive (`convert_virtual`).
  - **Removed:** the reason for going ahead (red heads-ups no longer need one; the field is gone from Allocation Confirmation), the "Confirm this
    box" action and its endpoints (mixed lines are ordinary lines; the "box cannot be completed" heads-up remains).
- **Changes after review 3:** the variety list no longer uses a progress bar. Each row shows a plain "N% covered" chip (everything ordered that is
  already allocated, confirmed, or can be covered by what is available), then ordered / confirmed / available as numbers, and one sentence
  saying why ("Short by 15,961 stems"). The two cards scroll on their own, and a loader shows the moment a variety is clicked. The forecast is
  shown as both values with the sum between them ("the forecast says 239", "we count on 104: 239 less 57% ...").
- **Changes after review 4:** "How this works" removed. The "not enough" and "not sure" heads-ups only say what is wrong (how many were asked, how
  many can be covered and from where, how many would be short); the suggestion buttons "Confirm N, the part we are sure of" and "Use 62cm instead"
  are gone. "We count on" is now "Safe to promise".
- **Changes after review 5:** "Try it out" removed (the heads-up popup already checks before anything is saved, and the before/after added little).
  Each variety and length has two tabs: **Exact length** (orders for that length) and **Longer than ordered** (orders for the same variety at a
  shorter length that this stock could fill; confirming them there gives them the longer stem and raises the longer-length heads-up).
- **Changes after review 6:** confirmed lines can be seen and taken out. Each variety and length has a third tab, **Confirmed**, listing every order
  line confirmed against it (held in N buckets / expected from the farms, with a warning when a held bucket is missing) and an **Unconfirm** button
  per line; the variety list has a **Confirmed** view next to Needs attention and All, so a fully confirmed variety is no longer hidden.
  Speed: the variety detail only reads its own variety, forecast reads are shared across lengths within a request, and the received / harvested
  reads are kept for a minute, so reloading after a confirmation takes a fraction of a second (the first open of a day can take a few seconds on a
  site with a very large Stock Entry table).
- **Teams:** the team belongs to the order line. Every confirmation carries one (Allocation Confirmation > Team, a Packing Team), chosen in a dropdown on each
  customer's row (starting from the line's current team, else the last one you used); different lines in one confirm can have different teams, and a
  line cannot be confirmed without one. Taking a line out needs no team. A mixed box is packed by one team, so the team chosen on one of its lines is
  saved on every line of the box (the other rows of the box follow on screen), and a later confirmation of another variety in the box picks it up.
  For allocation, `allocation_plan.teams_for_lines(sois)` returns `{sales_order_item: team}`, the map `allocate_stock_with_buckets` already takes to
  stamp a team on a pick list; `team_for_line` / `group_team` read a single one.
- **Swap or split:** when stock is already confirmed for other customers and someone is short, the Confirmed tab lists who holds it, and each
  customer's row has **Swap or split**. A dialog asks who gets the stems (any other customer of the same variety who still needs some, including
  those who ordered a shorter length), how many, and the receiver's team. Giving all is a swap (the giver goes back to "still to confirm"); giving part is
  a split. Stock held on the shelf moves first, then what was confirmed from the expected harvest (`allocation_playground.move_stems`, which re-confirms both
  lines with their new totals so holds, Confirmed Stems rows, buckets and teams stay consistent). A mixed box can receive stems (its team then applies to the whole box) but cannot give them away, because its lines are packed together.
- **Delivery date filter:** the date is set like on every other v2 page: a filter bar with Today / Tomorrow pills and a date box labelled **Delivery date**
  (tooltip "Sales Order delivery date"). The page opens on tomorrow by the server's clock (the day before shipping), the choice is kept in the address
  (`?date=`), and the numbers and the variety panel say "delivery".
- **Swap or split, several customers:** the dialog can give one customer's stems to several customers in one go ("Add another customer"; each row has its own
  stems and team; `allocation_playground.split_stems`). What you type is never changed silently: choosing a different customer keeps your number unless that
  customer cannot take it, and then it says why ("X only needs 198" / "only 302 is left to give"). Each row shows how much it can take, and the line
  underneath says whether this is a swap (everything to one customer) or a split (and what the giver keeps).
- **The stems box stops at what the order allows:** typing more than the order is for (less what is already allocated) brings the number down to the maximum at
  once and says why ("This order is for 6,000 stems, the most you can confirm", or "Only 5,200 left to confirm: 800 of this order is already allocated"); a negative
  number becomes 0. Fill every line, Use first and Swap or split already respected the same limit; the server still refuses anything above it.
- **Sales farm and stock farm (correction):** a plan is for a **sales farm**, the farm that processes the order, and only the farms marked as a sales shelf in
  Production Settings (today Kapkolia and Karen) are offered in the filter at the top, labelled "Sales farm". Stock is not limited to that farm: **On the
  shelf** and **Received, not shelved** count every farm in the sales farm's location and show them farm by farm ("Kapkolia 500 · Chepsito 1,200"). Each
  customer row has a **From** dropdown, the farm whose stock the confirmation is against (the farm with the most stock is preselected; a line keeps the farm
  it was confirmed against). The stems are held from that farm's shelves and received buckets, `Allocation Confirmation.farm` and the Confirmed Stems row
  carry that farm (so the allocator, which already lets several farms confirm parts of one line, sees it), and the plan itself belongs to the sales farm.
  Swapped or split stems keep the farm they were confirmed against. The forecast stays at location level.
- **Phase 3:** not started.

## 12. Decisions taken (defaults) and skipped

Taken by you: Real reserves buckets and stems; Virtual reserves stems only; allocation releases reservations; reservations are stored
for easy bucket lookup; the UI is minimalist.

Skipped for now: who can override or release a reservation (default: the planner who made it, and Packhouse Manager);
what to do when a reserved bucket is a ghost (default: mark Broken and alert).

Assumptions (change if wrong): one plan per delivery date and stock farm; sales planners confirm; the horizon is the setting
`allocation_horizon_days` (seeded at 3); offline orders are not tagged yet and count as demand.

## 13. Acceptance and measures

Baseline above. Targets after four weeks of use: confirmed lines on planned days 80% or more (from 0.4%); unfilled-while-stock-covered stems
down by half; avoidable discards (exact and substitute) down by half; downgrade regret under 10k stems per 3 weeks; broken mix groups 0;
revised-forecast error per variety tracked weekly.

Tests (following `tests/test_v2_allocation_planning.py`): reservation hides buckets from other lines and not from the owner; allocation
consumes and releases; concurrent reserve of one bucket yields one winner; each release trigger; guards for each rule; conversion order; the
board's KPIs equal the sum of its pools.

## 14. Risks

- Shelf stock is not trustworthy until ghost buckets are handled; Broken reservations make that visible but do not fix it.
- Variety-level forecast error is large; the safe/expected split and the over-promise guard are the mitigation.
- 34% of ordered stems have no spec, so guards for them are limited.
- Adoption was 0.4%: the board must be the allocators' and planners' default start, and must be fast.
