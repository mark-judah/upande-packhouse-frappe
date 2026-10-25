# 09 Allocation: eligibility, timing, Bucket Allocation Status, stale reservations

Read-only research. PH = `apps/upande_packhouse/upande_packhouse`, Q = `apps/upande_quality/upande_quality`, SA = `PH/upande_packhouse/page/sales_allocation/sales_allocation.py`, MOB = `PH/mobile/api.py`, QM = `Q/mobile/api.py`, CORE = `PH/api/v2/core/stock.py`.
Tags: **[V]** verified in code, **[L]** measured on the local DB (frozen snapshot), **[?]** uncertain or inferred. Local `now()` is 2026-10-25 but the stock/pick data are a snapshot of about **2026-10-13 11:42** (last bulk `Shelf Item.date_added`; pick rows exist only for OPLs created up to 10-11, OPLs of 10-12/10-13 have no rows). Every "as of" SQL below therefore uses `@asof = 2026-10-13 11:42`; in production replace it with `NOW()`.
SQL files used: `/tmp/claude-1000/-home-jk-Projects-upande-local-bench-v16/17d427cc-b5a7-41af-a956-596b17b9ecd7/scratchpad/alloc/` (`taxo.py`, `stale.sql`, `dash_stale.sql`, `q.py`).

## 0. Headlines (what management should know)

1. **Allocation is a manual, human step on the Desk page `sales-allocation` (roles Sales User / Representative / Manager / Master Manager / System Manager)**, one Sales Order at a time. There is no auto-allocator and no allocation job. The "auto-allocate" button is client-side FIFO over the page's own bucket list (SA js `_execute_fifo`:2569). A line cannot be allocated until a farm has **confirmed stems** for it (Allocation Planning page, `api/allocation.py:confimSalesOrderItem`:284). So the order of waiting is: not confirmed -> not allocated -> OPL draft -> issue -> pack.
2. **The server does NOT re-check eligibility.** Age, discard hold, cooling and length are filters in the page query only (SA:733-752, 844-857). `_allocate_stock_with_buckets_impl` (SA:1539) only checks: bucket exists on a farm of the location (1735+), confirmed stems cap (1594), box capacity (`_fit_allocations_to_boxes`:1465) and over-allocation vs outstanding rows (1781). So "ineligible but allocated" stock is possible (stale page, API caller), and the stock pages cannot assume the allocator enforced anything.
3. **Issuing does not free the Bucket Allocation Status (BAS) row. It settles it**: `Bucket Allocations.issued=1` on that line's rows, `allocated_quantity` re-summed over outstanding rows, `total_quantity` re-synced to shelf stems (MOB:5227-5262). The BAS row itself is never deleted by any code path in `upande_packhouse`/`upande_quality` except the manual `clearOplAllocations` script (QM:16). The comment at QM:3858 says BAS is "cleared-nightly", but **no job, hook or Server Script that does this exists in the repos or on the local site [V]** -- see section B.7 for the one production query that settles it.
4. **Reservation is released correctly only on the happy path** (full issue through the scan or Issue Offline, with matching length). It is left behind by: OPL cancel/delete (no hook), Sales Order cancel (no hook), Shelf Clear (never touches BAS), Discard (refused if any BAS row exists), draft OPLs never submitted, partial issue (see B.2), settle failures swallowed by `try/except` (MOB:5253), a pick row whose length differs from the BAS length (downgrade replacement, MOB:5230-5233 [V code, not reproduced locally]), and bucket-id reuse. Local: **43 outstanding allocation rows / 4,730 stems belong to lines whose pick rows are ALL issued** (should be settled), 9 rows / 630 stems have no pick row at all, 194 rows / 27,414 stems sit on live OPLs whose delivery date has passed, 15 rows / 2,060 stems on draft OPLs, 139 BAS rows / 13,910 stems have no shelf row (ghost).
5. **Biggest idle-team lever found in the stock side (local numbers, caveats in section 6.3):** open Discard Requests hold whole buckets (68% of local shelf stems, 777,170 of 1,144,604), and `createDiscardEntry` refuses to discard any bucket that has a BAS row, so a bucket that is both allocated-at-some-time and listed for discard is held with no way out (113 such buckets locally).
6. **Stock pages do not all agree.** v2 pages (Stock Visibility, Avails, Allocation Planning, Cold Room) share `core.stock.bucket_rows`; the v1 pages (`api/stock_visibility.py`, `api/avails.py`), the Quality app candidate lists and the Shelving Target board (`getShelvingDemand`, QM:4768) each subtract BAS differently (table in B.5).

---
# PART A. ELIGIBILITY AND TIMING

## A1. What makes a shelf row allocatable

Candidate rows = `Shelf Item` rows (grouped per bucket, variety, length, shelf) joined to `Shelf`; nothing that is not on a shelf can be allocated (received-not-shelved, trolley, truck, quarantine are all invisible to the allocator; only an info badge `incoming_exact_stems` counts received-not-shelved exact-length stems, SA:1200-1300).

| Rule | Where | Detail |
|---|---|---|
| Farm scope | SA:526-545 | `location` (Farm.farm_location of the **enabled** Shelf Locations rows, `_get_production_config` SA:59) -> `location_farms`. Default list = the location's `sales_shelf=1` farm only (Kapkolia, Karen in production); remote farms (Chepsito, Kaptumbo, Simotwo, Torongo, location Kapkolia) appear only if the planner ticks them (`selected_farms`). Farms with no `farm_location` or not in Shelf Locations are not allocatable at all. |
| Remote bucket | SA:1752, 1811; SA:551 | Allocatable but `needs_transfer` (shelf farm != sales-shelf farm): BAS.in_transit=1, pick row `awaiting_transfer=1`, **no ledger sale yet** (`move_allocation_to_sold` defers, stock_movement.py:997), OPL stays draft until the bucket is shelved at the hub (`opl_submit_blockers` SA:2280). |
| Age | SA:733, 740, 1096 | Anchor = `COALESCE(Shelf Item.harvest_date, date_added)` as a date, `DATEDIFF(CURDATE(), anchor)`. Must satisfy `age < discard_age` (5) **and** `age <= farm max_allocation_age`. Since discard_age=5 is strict, production's max_allocation_age=5 is redundant: effective limit = 4 days (Simotwo: 2). core.stock mirrors it: `too_old = age > max_age or age >= discard_age` (CORE:142). Local settings are 4 at every farm (production differs, see brief). |
| Cooling | SA:752, CORE:143 | `hours_since_shelved = TIMESTAMPDIFF(HOUR, date_added, now)` must be `>= cooling_hours`. **cooling_hours = 0 everywhere in production, so the gate is OFF: stems shelved a minute ago are allocatable.** (`row.get("cooling_hours")`, SA:97; the Shelf Locations doctype is orphaned.) Note `Shelf Item.date_added` is re-set if a bucket is moved/re-shelved [?]. |
| Held (open Discard Request) | SA:27 `DISCARD_EXCLUSION`, `availability.reserved_bucket_ids` availability.py:29 | Bucket id on any `Discard Request Bucket` with `discarded=0` whose parent `workflow_state != 'Rejected'` (Draft, Pending Approval, Approved, anything). Per **bucket**, not per variety row. Matches bucket id only, no date: a **reused bucket id inherits an old unprocessed request** (local: 968 buckets / 171,079 stems were shelved AFTER the request that holds them, `Shelf Item.date_added > Discard Request.creation`). |
| Quarantine | not a shelf concept | Quarantined stems are in the Quarantine warehouse by Stock Entry, never on a Shelf Item, so they are simply "not shelved" (class b). `Release From Quarantine` Server Script / `releaseFromQuarantine` (QM:7333) moves them to Receiving storage; shelving follows. No quarantine test exists in the allocator or core.stock. |
| Already allocated | SA:710-731 | `available = stem_qty - SUM(Bucket Allocations not cancelled, not issued)` recomputed from child rows (not the stored BAS field). Row hidden when available <= 0 unless allocated to this line (SA:857). |
| Variety | SA:578-580, 815 | Exact `item_code` match. **No variety substitution rule**: the planner swaps the order line's variety with `substitute_variety` (SA:3776; blocked if the line has ANY non-cancelled allocation incl. issued ones, SA:3822, or is on a submitted OPL). Logged as Error Log "Variety Substitution". |
| Length | SA:822-851 | `_parse_cm`; bucket length == line `custom_length` -> **exact**; longer -> **downgrade** ("upgrade" of stems into a shorter-ordered line); shorter or unparsable -> dropped. Exact sorted before downgrade. Downgrade of a bucket younger than `amber_time` (3 days) asks the user for a typed reason ("requires_approval", client dialog only, stored in `Pick List Item.downgrade_reason`); at/after amber_time the reason is auto "Amber time expired" (SA js:2317-2330, 2584). No real approval step. |
| "Exact length" fields | SA:1222, `available_stems_of_exact_length` | `incoming_exact_stems` (received, unshelved, same length) is copied to the pick row so packing can see the downgrade was chosen while exact stock was inbound. |
| Cut stage | SA:815-835 | Line `custom_cut_stage` must equal bucket `cut_stage` (normalised) unless "bypass cut stage" is ticked. Not in core.stock. |
| Mixed box / bunch | `_create_pick_list` SA:1915; `opl_submit_blockers` SA:2310-2340 | Same stock rules per line; the OPL is per mix_group / bunch_group, so **the whole group must be fully allocated before the OPL submits**: one short colour keeps the entire mixed OPL draft. |
| Packrate / box capacity | `_line_capacity` SA:1454, `_fit_allocations_to_boxes` SA:1465 | capacity = (packrate or mixed packrate, default 10) x number_of_boxes. Last bucket is drawn partially, rest stays free; line already full -> error "Line already full". No rounding to whole bunches. |
| Confirmed stems | SA:610-620, 1594 | If **any** line of the order has confirmed stems, lines without confirmation at this location are not listed; allocation cannot exceed the farm's confirmed stems. Required stems for OPL submit use the order's **global confirmed total** (SA:2290), so a partly confirmed order can submit "complete". |
| "Over-allocation on bucket X: 0.0 available, N requested" | SA:1781-1786 | `requested > live_shelf_qty - outstanding_rows` after re-sync. Causes: concurrent allocator, stale page, **stale outstanding rows on a reused bucket id** (hidden from the page too, SA:857). `frappe.throw` does not write an Error Log, so it is not measurable from logs. |

**Preference order (what the page offers and FIFO picks):** `sort_key` = (is the farm with the most exact stock first, then **oldest age first**) (SA:901-905); exact before longer. So the allocator is **FIFO by age, within the "preferred" (biggest-exact-stock) farm**. Freshest stock is used last. There is no server-side preference.

## A2. Timing: who, when, how late

- **Who**: Sales-role users on `sales-allocation`; team per line is mandatory (`ALLOWED_ALLOCATION_TEAMS`, SA:1415; teams validated vs `Packing Teams`). Planners confirm per-farm stems first (Allocation Planning v2 page; write via `confimSalesOrderItem`).
- **When**: default delivery filter is **tomorrow** (`default_delivery_date`, SA:169): allocation runs on the processing day (delivery - 1). Pending list: submitted SOs not Completed/Closed/Cancelled; without date filters only the last 7 transaction days (SA:296-330).
- **Local evidence [L]**: 1,144 of 1,196 OPLs were created on delivery-1 (95.6%), 14 on/after the delivery day; of the processing-day OPLs **16.5% (189) were created at or after 12:00**; creation hours peak 08:00-10:59 (785 of 1,144). Production brief says 15-50% after noon.
- **Allocation timestamps (where they live)**:
  - `Order Pick List.creation` = first allocation of that OPL (reliable for first allocation only).
  - `Pick List Item.creation` = **inherits the OPL's creation** when appended via ORM (0 differences in 6,065 rows [L]); top-ups to a submitted OPL are raw-SQL inserts (SA:2092) with `creation`/`modified` **NULL** (11 rows [L]) -- NULL creation = late top-up allocation.
  - `Bucket Allocations.creation/modified` = **the BAS row's** (child rows inherit; 0 differences in 305 rows [L]). A reused BAS therefore shows the OLD creation for a new allocation. `Bucket Allocation Status` has `track_changes=0`: no history of allocate/release.
  - `Error Log` titled "Incoming Allocation Request" is written on every allocation click (SA:1560, payload with SO, location, bucket list) and "Bucket Marked In Transit"; 28 and 25 rows locally. Best click timestamp, subject to Error Log retention [?].
  - `Stock Entry` "Move To Graded Sold" (custom_issued_to = line, custom_bucket_id) is posted at allocation for hub stock; remote stock is deferred to arrival (stock_movement.py:997).
  - `Sales Order` has `track_changes=1`: Version rows carry when Confirmed Stems rows were added (5 locally); `Confirmed Stems.creation` is the row's own when added to an existing order (12 of 47 differ from the SO creation [L]).
- **Lateness metrics**: hours from processing-day 00:00 to OPL.creation; share of OPLs created after 12:00; per OPL minutes from first allocation to first issue (`COALESCE(issued_at, modified)`, doc 05) and to first pack list (production brief: median ~1 h and ~2 h).
- **Partial / short allocation**: allowed (any qty up to confirmed). OPL is created **draft** and submits only when `opl_submit_blockers` is empty (SA:2245): rows exist; no row `awaiting_transfer`/`in_transit`/(`loaded_in_trolley` and not shelved); every line of the OPL (or the whole mix/bunch group) cumulatively allocated >= required (confirmed total else ordered); unless `short_accepted` (then only the transfer blocker applies, SA:2296). Draft OPLs can still be issued; `_submit_for_packing` (MOB:2100) submits a draft once nothing is on a truck and issued stems >= `pack_waiting_replacement_pct` (default 50%) or all found rows are issued. Local drafts: 86 (73 are 10-13 rows-less test OPLs, 11 old drafts of 09-11..10-08).
- **Compression**: late allocation moves everything downstream (draft OPL -> issue -> pack list); issuing is also gated by the Packhouse Schedule per team (`_schedule_blockers`, MOB:4891) so a late #1 order delays #2..n of the same team.

## A3. States where an order waits for stock, and what users do

| State | Visible as | What users do today |
|---|---|---|
| Not confirmed by any farm | Allocation Planning `open`; line absent from the allocation page when other lines are confirmed (SA:610) | farm planner confirms stems |
| Confirmed, shelf empty for variety/length | Page shows no batches; Diagnostics (below) shows nothing | wait for harvest/receiving; or `substitute_variety`; or accept a longer length (downgrade) |
| Stock exists but not shelved (received/trolley/truck/quarantine) | `incoming_exact_stems` badge only | wait for shelving; push shelving |
| Stock on shelf but held / too old / cooling / wrong farm | `get_bucket_visibility_diagnostics` (SA:938) buckets them in this priority: pending_discard, remote_farm (not selected), cooling, past_discard_age, past_max_age, too_short, wrong_cut_stage, fully_allocated, eligible | quality manager resolves discard; planner ticks the remote farm; accept downgrade; use bypass cut stage |
| Allocated from remote farm | OPL draft; pick rows awaiting_transfer | transfer scheduling (doc 07); packing waits for 50% pack-while-waiting |
| Partially allocated | OPL draft, `custom_fully_allocated=0` | allocate more later (`_append_rows_to_existing_opls` raw insert on submitted OPLs, SA:2040) |

`get_bucket_visibility_diagnostics` is an existing, tested "why no stock" classifier per line; the dashboard can reuse its REASON_META (SA:1079) verbatim for the reason class. Its allocated figure uses the stored `bas.allocated_quantity` while the page uses recomputed rows (they match locally: 0 mismatches over 285 BAS).

## A4. 0-day-age allocation (harvested/shelved today)
- Allowed: age 0 satisfies both age tests and cooling is off (A1). Because the order is FIFO-oldest-first (A1), 0-day stems are allocated only when older matching stock is exhausted, held, or on a non-preferred farm; the allocator on the processing day (09:00-13:00) is mostly drawing stems harvested the same morning.
- Consequences: stems shelved minutes ago are issued within ~1 h of allocation (production median) with no cold-chain time; bucket may still be being filled/graded [?]; remote 0-day buckets additionally need the truck.
- Measure: `DATEDIFF(DATE(allocation_time), COALESCE(harvest_date, date_added))` = 0 and `TIMESTAMPDIFF(HOUR, shelved_on, allocation_time) < 6` (cooling bypass). Local BAS (285) shows 237 rows with BAS.harvest_date = BAS.creation date, and all 131 matched Shelving Logs were allocated <1 h after shelving **[L, mostly JTEST/KPKFLOW test data, not indicative]**. In production use `Error Log` / OPL.creation as the allocation time (BAS.creation is wrong for reuse).

## A5. Discard requests and allocated orders
- `upande_quality.tasks.auto_discard_request` (cron `0 0 * * *`, Q/tasks.py:5): per shelved Shelf Item with `age >= discard_age` (age from the **latest Harvesting Stock Entry posting_date** for the bucket id, else date_added; note a different anchor than the allocator's Shelf Item.harvest_date), creates one request per farm and **auto-approves** it (Draft -> Pending Approval -> Approved, submit). Skips buckets already on an open request. **It does not skip allocated buckets** (the manual work-list does, QM:3858) -> `allocated_in_held` (CORE:132).
- Effect on availability: from approval on, the whole bucket is `held` for all v2 pages and excluded by the allocator query; it stays held until a farm user scans `createDiscardEntry` (QM:449), which sets `discarded=1` on all rows of the bucket (QM:736). **That scan is refused if the bucket has any BAS row** (QM:607 `is_bucket_allocated`, used at QM:784) -> reason `bucket_allocated`. So an allocated-then-aged bucket, and any bucket whose BAS row was never deleted, cannot be discarded and stays held for good.
- Effect on already-allocated orders: the pick row is untouched; the allocation page no longer lists the bucket (DISCARD_EXCLUSION) so the allocator **cannot unallocate it from the UI** either; issuing still works if the stems are physically there (issue does not check holds). Visible only as `allocated_in_held` in core.stock.
- Local: 3,799 approved-not-discarded rows (8 requests of 09-25: 2,810 buckets, 1 discarded; 2 of 09-30: 989), plus 3 on 10-23. 777,170 of 1,144,604 shelf stems are held [L]; 3,797 buckets; of the held stems 171,079 sit in buckets shelved after the request (probable reuse artifact). 113 held buckets also have a BAS row (cannot be discarded). **These numbers are a frozen snapshot; production farms process discards daily, so the production share is likely far smaller [?].**

## A6. "Why no stock" taxonomy with SQL (core.stock.bucket_rows semantics)

Waterfall per (variety, required length, order region) cell on the unallocated lines of one delivery date. Supply = shelf rows with length >= required (exact + downgrade), farm in the order's region (Karen = Karen; Ravine = Kapkolia, Torongo, Simotwo, Chepsito, Kaptumbo). Short stems are explained in this order:

| Class | Definition (priority order) | Threshold / SQL element |
|---|---|---|
| c1 held | bucket on open discard request, request older than the shelf row | `bucket_id IN (SELECT drb.bucket_id ... workflow_state<>'Rejected' AND discarded=0)` |
| c2 held (reuse) | same, request created BEFORE the row was shelved | `dr.creation < si.date_added` |
| c3 too old | `age > max_alloc(farm)` or `age >= 5` | `DATEDIFF(@asof, COALESCE(harvest_date, DATE(date_added)))` |
| c4 cooling | `hours_on_shelf < cooling_hours` | 0 hours in production, so always 0 |
| f allocated elsewhere / stale | free stems = shelf - BAS.allocated (matched on bucket+variety+length) | see B part for stale split |
| b not shelved yet | Receiving/Late Receipt SE (last 2 days) whose bucket has no Shelf Item and no Shelving Log since posting_date; b2 = Harvesting SE with no later Receiving | |
| e other farm/region | matching stock at farms outside the order's region | `r.farm NOT IN region` |
| d wrong length | same variety, shorter than required | `cm(stem_length) < cm(required)` |
| a nothing | remainder = empty shelf, waiting for harvest | |

Local run (script `taxo.py`, as of 10-13 11:42, production-like max_age 5 / Simotwo 2, cooling 0). Lines of delivery 10-14 **without an OPL** (239 of 451 lines; the 212 lines that already had an OPL are skipped, and their allocations are not in BAS locally so competition is understated): demand 89,410 stems, covered by allocatable stock 45,126, short **44,284**: c1 held 34,435; c2 held-reuse 1,070; c3 too old 570; c4 cooling 0; f allocated elsewhere 0 (not measurable locally); b not shelved 4,696; e other region 160; d wrong length 983; a empty 2,370. Of the 45,126 "covered" stems **17,605 exist only on remote farms** (need a transfer, OPL draft). For delivery 10-13: demand 146,322, short 38,586 (c1 31,236, c2 1,720, c3 1,340, b 1,195, b2 180, d 805, a 2,110). **Magnitudes are driven by the unprocessed local discard requests (A5) and are not a production estimate.**

Production-ready SQL pieces: the variety/length/farm shelf split is `core.stock.bucket_rows` + `summarize` (use the API, not raw SQL, for stock); demand = `SELECT soi.item_code, soi.custom_length, so.delivery_date, SUM(soi.stock_qty) - COALESCE(SUM(ba.q),0)` with `ba.q = SUM(quantity_allocated)` where `cancelled=0`; the unshelved pieces are the two NOT EXISTS queries in `taxo.py`.

---
# PART B. BUCKET ALLOCATION STATUS, BUCKET ALLOCATIONS, ORDER PICK LIST

## B.1 Structure and how reserved quantity is computed
`Bucket Allocation Status` (BAS; autoname hash, no track_changes, no controller logic): `bucket_id` (Link Bucket QR Code), `item_code`, `stem_length`, `warehouse`, `farm`, `shelf_location`, `shelf_farm`, `harvest_date`, `in_transit`, `total_quantity` (snapshot of shelf qty), `allocated_quantity`, `available_quantity`, `receiving_entry`, child table `bucket_allocations`.
`Bucket Allocations` (child): `sales_order`, `sales_order_item` (Data), `quantity_allocated`, `cancelled` (Check), `issued` (Check, read-only).
- **One BAS row per (bucket, variety, length)** (keys at SA:1700+, bas_map). Created at first allocation of that combination, kept forever; one child row per (order line) allocation; `existing_so_items` skips a second row for the same line (SA:1788).
- **Reserved (outstanding) = SUM(quantity_allocated) of child rows with cancelled=0 and issued=0**, re-derived by `recompute_bas_quantities` (SA:1382-1406) which also sets `total_quantity=live shelf qty` and `available_quantity = max(0, total - allocated)`. Local check: stored `allocated_quantity` == outstanding sum for all 285 rows [L].
- **How availability uses it**: core.stock `bucket_rows` (CORE:105-143) reads stored `allocated_quantity` grouped by (bucket, item_code, stem_length); `allocated = min(alloc, stems)`, `free = stems - allocated`, `over_allocated = alloc - stems`; if the bucket is held, allocated is shown as `allocated_in_held` and free=0. The allocator page recomputes from child rows (SA:710-731). `availability.variety_availability` (availability.py:62) subtracts stored allocated, uncapped, keyed without length.
- Coverage (not reservation) of an order = SUM(child rows with cancelled=0) INCLUDING issued rows (`_cumulative_allocated_stems` SA:2221, pipeline.py:266): an issued row still counts as the line's allocation, but not as bucket reservation.

## B.2 Release paths, traced (answers "does issuing free the bucket?")

| Path | Code | Effect on BAS / child rows | Verdict |
|---|---|---|---|
| **Issue scan** `issueBucketToSaleOrderItem` | MOB:4878; settle 5227-5262 | For each (variety,length) with newly issued pick rows: finds BAS by `bucket_id + item_code + stem_length(=pick row length)` (5230-5233) -> **all** non-cancelled child rows of that line set `issued=1` (even if the pick row drew fewer stems than allocated), `recompute_bas_quantities(shelf_qty=stems left)`. Shelf Item decremented per drawn stems or deleted when empty (5120-5215). BAS row stays. | Released for the line (as a whole, not proportionally). **Left behind** when: no BAS found because length differs (downgrade replacement rows carry the ORDER's length, BAS keeps the bucket's, SA:3467-3469); no unissued pick row exists (`issued_by_variety` empty, e.g. row deleted, issued by another path); settle raises (logged "Issue: settling Bucket Allocation Status failed", swallowed). Same length mismatch also skips the shelf decrement (5159-5170) [V code, not reproduced]. |
| Issue offline / mark issued offline | `offline_issue._issue` (offline_issue.py:236) calls the same scan endpoint | identical to above | same |
| Issued less than allocated (partial) | `issued=1` set on the whole child row | **over-release**: if 100 allocated and the pick row drew 40, the remaining 60 are released from BAS reservation but also left on the shelf; and if two lines share a bucket each line's child row is issued on its own scan | partially released |
| Unallocate button | `unallocate_bucket_from_opl` SA:2491 | refused if any child row issued (2526); else reverses the Sold ledger leg, sets `cancelled=1` on that line's rows, recompute, `in_transit=0` if none left, raw-DELETEs the line's pick rows, deletes OPL if empty (`_force_delete_opl`), resets SO line flags | **fully released**, but reachable only for buckets the page lists (on shelf, age ok, not held, not ghost) |
| Replacement `_replace_requested_bucket` | SA:3251; BAS 3347-3395 | old BAS child rows of the moved lines `cancelled=1`, recompute; new BAS row claims the stems; pick rows re-pointed; old shelf row cleared unless keep_old (3390) | released for the old bucket; new bucket reserved. Old row is "cancelled" (the only normal user of the flag besides unallocate) |
| Packing reject + donor (`packing_reject`) | packing_reject.py:436-452 | donor gets an **already issued** row (never outstanding); only if the donor has a BAS row | clean |
| Packing quality replacement | `_shrink_old_allocation` packing_quality.py:538 | old allocation shrunk, row cancelled when 0; new allocation through the page core | clean |
| OPL **cancel / delete** (Desk) | `order_pick_list.py` only has `before_submit` (blockers) and `on_trash` (transfer blockers); hooks.py:268 only `after_insert`/`on_update` | **nothing releases BAS**; SO line `custom_opl` left dangling; Sold-warehouse ledger move left in place | **left behind** |
| Sales Order cancel / close | no hook (hooks.py:244) | nothing | **left behind** (the pending list drops it, the stock stays reserved) |
| Discard | `createDiscardEntry` QM:449 | refused when a BAS row exists (QM:607/784); otherwise deletes the shelf row | cannot release; blocks discard instead |
| Shelf Clear (emergency Server Script, role "Shelf Clearer") | local Server Script "Clear Shelf Item (emergency, role-gated)" | deletes Shelf Item, closes Shelving Log; **never touches BAS** | allocation left on a bucket that has no shelf (ghost); doc 10: 21,119 rows cleared, 1,106 pick rows cleared after allocation |
| Offline removal `createOfflineIssuingEntry` | QM ~12960 | refused when a BAS child row is outstanding | clean |
| Coldroom reject `saveColdroomReject` | QM ~14140 | lowers BAS total/available only, allocated untouched | can create `over_allocated` |
| Wrong-variety correction | `offline_issue._correct` | re-keys BAS only when allocated=0 | n/a |
| Bucket re-harvested in a new cycle | bucket id is a reusable container | BAS keyed by id+variety+length is **not reset**; stale outstanding rows attach to the new stems if variety and length are the same (ST-6 only protects different variety) and show as "0 available" / over-allocation | left behind |
| `clearOplAllocations` Server Script/API | QM:16 | deletes **every** BAS (all orders' allocations on those buckets) that has a row for the OPL's order, force-deletes OPL; no role check beyond whitelist | manual sledgehammer; the only code that deletes BAS |
| draft OPL never submitted | no timeout | child rows stay outstanding as long as the OPL exists | held indefinitely |
| OPL `short_accepted` / completed short (`completeOplShort` MOB:2160) | flags only | unissued, not-found rows' allocations are not released | left behind |

**Direct answer:** Issuing does not delete or "free" the bucket from Bucket Allocation Status; it marks the line's child rows issued and recomputes, which removes them from the outstanding reservation. That works for the matching-length, matching-row happy path and fails silently otherwise. Nothing ever deletes a BAS row; this is why `createDiscardEntry` (any BAS row = "allocated") and `getShelvingDemand` (all BAS rows) keep seeing old allocations.

## B.3 Stale allocations: classes, how long they hold stock, who sees them
Classes (SQL in B.6; local counts as of 10-13 on outstanding child rows, 263 rows / 34,854 stems):
| Class | Rows | Stems | Hold shelf stock? |
|---|---|---|---|
| live: submitted OPL, rows still to issue, delivery not past | 2 | 20 | yes (legit) |
| draft OPL (never submitted) | 15 | 2,060 | 12 rows / 1,800 stems on a shelf |
| settled-not-released (all pick rows issued) | 43 | 4,730 | no, all ghost (shelf row gone) |
| orphan (no pick row for bucket+line) | 9 | 630 | no, ghost |
| live OPL, delivery date already past | 194 | 27,414 | **109 rows / 19,134 stems still on a shelf** |
| order cancelled / closed / missing | 0 | 0 | |
(1 live-class row sits on a reused bucket id.) Ghost (no shelf row): 139 BAS / 13,910 stems; on shelf and covered by shelf stems: 122 BAS / 20,944 stems; "over-allocated" (allocation greater than shelf) 0 rows locally in this cut but doc 03 counted 139 buckets / 13,910 stems.
Holding time: **indefinite** (no expiry); only an unallocate, a re-allocation to a new line on the same row (not possible, stems are reserved), a settle on issue, `clearOplAllocations` or direct DB edit ends it. A held stale allocation also blocks `substitute_variety` for that line (SA:3822) and the bucket's discard (QM:607).

Which function subtracts allocations, and whether they treat stale rows the same:
| Consumer | Function | Subtracts | Differences |
|---|---|---|---|
| Stock Visibility v2, Avails v2, Allocation Planning v2, Cold Room v2 | `core.stock.bucket_rows` (CORE:60) | stored `allocated_quantity` per (bucket, variety, length), capped at stems present, **only for buckets with a live shelf row**; ghost rows hold nothing | consistent with each other (aligned earlier). Cold Room v2 shows `free` (not allocatable): too old/cooling stems count as free there |
| Allocation page | SA:688-731 | recomputed outstanding child rows per (bucket, variety, length, shelf) | same figure when stored == recomputed |
| Stock Visibility v1 | `api/stock_visibility.py:62-77` | `SUM(bas.allocated_quantity)` where a shelf row exists, grouped by **`bas.shelf_farm`** (BAS snapshot farm, not the shelf's), by variety+length without capping | farm can differ; over-allocated rows inflate |
| Avails v1 | `api/avails.py:40-55` | same, no `allocated_quantity>0` filter | same |
| Discard (v1 stock pages) | `docstatus<2`, `is_shelved=1`, `discarded=0`; a Rejected request still counts | differs from `reserved_bucket_ids` (workflow_state != Rejected) | |
| Cold Room v1 | `api/coldroom.py:240-262` | "allocated today" = unissued Pick List Item stems on submitted OPLs with `date_created = today` | **pick rows, not BAS**: a different reservation concept |
| `availability.variety_availability` (SO spec popup, legacy) | availability.py:62 | stored allocated, not capped, no length key, farm = Shelf Item.farm | counts stale allocations and too-old stock |
| Quality app candidate lists | QM `gradingReplacementOptions` 6034, `listBunchDestinations` 6395, `listReplacementCandidates` 6643, `replaceBunchInOpl` 8068 | `stem_qty - allocated_quantity` joined on bucket+variety (no length) and **exclude `in_transit=1` buckets entirely** | in-transit allocated buckets are unavailable here but allocatable on the page |
| **Shelving Target board** `getShelvingDemand` (QM:4768) | `coverage = max(on_shelf, SUM(all BAS.allocated_quantity))` per variety (4901) | every BAS row of any age/order (company-wide or per BAS.shelf_farm) | stale/ghost allocations **reduce the number of stems shelvers are told to shelve** whenever allocated > shelf; no held/too-old/length filtering |

## B.4 Why 305 Bucket Allocations vs 6,065 Pick List Items [L]
- Of 6,065 pick rows, those of OPLs created 09-04..09-24 (+10-23) mostly have a matching BA (e.g. 103/126 on 09-11, 126/126 on 09-24, 10/10 on 10-23), but **pick rows of OPLs created 10-01..10-11 (about 5,650 rows; only 18 of 437 on 10-08) have none**. Same snapshot gap as the BAS: those OPLs came from production replay with allocation tables cleaned/not copied, or **production clears BAS/BA nightly [?]** (QM:3858 "cleared-nightly").
- In the allocator flow every pick row is written with a BA child in the same transaction (SA:1788 + `_create_pick_list`), so a pick row without BA only arises from: raw-SQL top-up with BA (still created), manual OPL creation, unallocate/replacement races, deleted BA/BAS (`Deleted Document`: 9 BAS on 09-19), or the replay gap. **Pick rows are NOT the reservation in any stock page except Cold Room v1**; but when BA/BAS are missing the stock pages show those stems as free. Local measure: **131 shelf rows / 16,901 unissued pick stems on live OPLs have no BAS allocation at all** (invisible reservations), 121 rows have BAS >= pick stems, 1 BAS < pick.
- Consequence for the dashboard: compute "reserved" from pick rows AND from BAS and show the difference ("reservation drift").

## B.5 Other factors on Stock Visibility numbers (function that applies each, are pages aligned?)
| Factor | Applied by | v2 pages (SV, Avails, AP, Cold Room) | Others |
|---|---|---|---|
| Held discards | `reserved_bucket_ids` availability.py:29 (per bucket; rows not discarded; parent state != Rejected) | same | v1 pages use docstatus<2 + is_shelved; Quality lists use their own NOT IN subquery |
| Quarantine | not modelled | not on shelf, so absent | n/a |
| Cooling | CORE:143 vs SA:752 | `allocatable` excludes; `free` includes; Cold Room shows `free` | 0 hours in production: no effect |
| max_allocation_age / discard_age | CORE:142 | `allocatable` only; Stock Visibility "on shelf" counts them | allocation page excludes (SA:733-740) |
| sales_shelf | not applied by core.stock (stock is by any shelf farm); allocator default list = sales_shelf farms, remote needs ticking | AP uses `farm_set` of the order region, not the allocator's active farm list: AP "available" includes remote stock the planner may not have ticked | `farm_config` only has **enabled** Shelf Locations farms: stock at farms with no row falls back to defaults max 5, cooling 0 (CORE `fc.get(...,{})`) |
| Ghost shelf items | filters `stem_qty>0` and bucket/variety present (CORE:63) | rows with 0/NULL stems dropped (6 locally); rows whose stems physically left but were never decremented are counted | partial issue on length mismatch leaves phantom shelf stems (B.2) |
| Farm filter semantics | CORE:70-85 HAVING on `MIN(s.farm)` of the grouped row | location = first farm alphabetically if a bucket/variety/length sits on two farms (18 groups, 6,800 stems locally) | v1 pages group by `Shelf Item.farm`/`bas.shelf_farm` |
| Duplicate bucket ids across farms | same grouping | counted once at MIN(farm) | allocator keys `(bucket, item, length)` within the location only (SA:1735) |
| Stale allocation subtraction | stored BAS, capped, live-shelf-row only | consistent | `getShelvingDemand`, v1 pages, Quality lists differ (B.3) |

## B.6 SQL for the dashboard (all read-only; local-tested unless marked)

**S1. Stock held by stale allocations, by variety / length / farm / age band, with class** (tested, file `dash_stale.sql`; replace `DATE('2026-10-13 11:42:00')` by `CURDATE()`; use `ba.modified` or OPL creation for age, NOT `ba.creation` which is the BAS creation):
```sql
SELECT cls, bas.item_code AS variety, IFNULL(bas.stem_length,'') AS stem_length, sh.farm AS shelf_farm,
       COUNT(*) allocations, COUNT(DISTINCT bas.bucket_id) buckets,
       SUM(LEAST(ba.quantity_allocated, sh.stems)) AS stems_held_on_shelf
FROM (
  SELECT ba.*, bas.bucket_id, bas.item_code, bas.stem_length,
    CASE
      WHEN so.docstatus IS NULL OR so.docstatus <> 1 OR so.status IN ('Cancelled','Closed') THEN 'order_cancelled'
      WHEN pl.n IS NULL THEN 'orphan_no_pick_row'
      WHEN pl.nopen = 0 THEN 'issued_not_released'
      WHEN o.docstatus = 0 THEN 'draft_opl'
      WHEN so.delivery_date < CURDATE() THEN 'delivery_date_past'
      ELSE 'live' END AS cls
  FROM `tabBucket Allocations` ba
  JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
  LEFT JOIN `tabSales Order` so ON so.name = ba.sales_order
  LEFT JOIN (SELECT bucket, IFNULL(NULLIF(custom_sale_order_item,''), sales_order_item) soi, COUNT(*) n,
                    SUM(IFNULL(issued,0)=0) nopen, MIN(parent) opl
             FROM `tabPick List Item` WHERE parenttype='Order Pick List' GROUP BY 1,2) pl
         ON pl.bucket = bas.bucket_id AND pl.soi = ba.sales_order_item
  LEFT JOIN `tabOrder Pick List` o ON o.name = pl.opl
  WHERE ba.cancelled = 0 AND IFNULL(ba.issued,0) = 0) ba
JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
JOIN (SELECT si.bucket_id, si.variety, IFNULL(si.stem_length,'') sl, MIN(s.farm) farm, SUM(si.stem_qty) stems
      FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name = si.parent WHERE si.stem_qty > 0 GROUP BY 1,2,3) sh
  ON sh.bucket_id = bas.bucket_id AND sh.variety = bas.item_code AND sh.sl = IFNULL(bas.stem_length,'')
WHERE ba.cls <> 'live'
GROUP BY cls, variety, stem_length, shelf_farm ORDER BY stems_held_on_shelf DESC;
```
Add the age band as `DATEDIFF(CURDATE(), so.delivery_date)` (days past delivery) or days since `o.creation`. Local result (as of 10-13): delivery_date_past Moonwalk 42cm Kapkolia 8d+ 54 buckets 10,800 stems (all September test data), Athena 42cm 3,184, Copacabana 1,440, ...; draft_opl Ever Red 62cm Torongo 1,540 stems (OPL-2026-00056, 18 remote rows awaiting transfer, delivery 10-09).
**S2. Ghost allocations** (tested): BAS with `allocated_quantity>0` and no `Shelf Item` of same bucket+variety+length with `stem_qty>0`: 139 / 13,910 stems [L]. Use `LEFT JOIN (shelf group) sh ... WHERE sh.stems IS NULL`.
**S3. Settled-not-released** (class `issued_not_released` in S1, includes ghosts): 43 / 4,730 [L]. Dashboard action: "re-run settle" (call `recompute_bas_quantities` after marking issued) is a fix, not a metric.
**S4. Reservation drift (pick rows without BAS)** (tested): per (bucket, variety, length) on shelf: `SUM(Pick List Item.stock_qty) WHERE issued=0 AND not_found=0 AND OPL docstatus<2` vs `SUM(BAS.allocated_quantity)`; 131 rows / 16,901 stems with BAS=0 [L].
**S5. Held but cannot be discarded**: buckets on open requests (`workflow_state<>'Rejected' AND discarded=0`) that also have any BAS row: 113 buckets [L]. Reuse-hold: bucket on open request with `dr.creation < si.date_added`: 968 buckets / 171,079 stems [L].
**S6. Shelving-board inflation**: per variety `GREATEST(SUM(bas.allocated_quantity) - on_shelf_stems, 0)` = how much shelving target is hidden (0 locally because shelf stock is large; check in production per farm: farm-scoped shelf vs BAS.shelf_farm).
**S7. BAS existence and age (run first in production)**: `SELECT COUNT(*), MIN(creation), MAX(creation), SUM(allocated_quantity>0), SUM(allocated_quantity=0) FROM \`tabBucket Allocation Status\`;` and `SELECT deleted_doctype, DATE(creation), COUNT(*) FROM \`tabDeleted Document\` WHERE deleted_doctype IN ('Bucket Allocation Status','Order Pick List') GROUP BY 1,2;` If BAS spans months, nothing clears it nightly; if it only spans today/yesterday, an external job does and the stale-class analysis changes to "what the job misses".
**S8. Allocation lateness** (tested): `Order Pick List o JOIN Sales Order so`: share with `DATE(o.creation)=so.delivery_date-1`, share with `HOUR(o.creation)>=12`, hours between processing-day 00:00 and creation; local Kapkolia 16.5% after noon (189/1,144).
**S9. Cooling bypass share**: allocations (OPL creation or Error Log "Incoming Allocation Request") where `TIMESTAMPDIFF(HOUR, latest Shelving Log.shelved_on, allocation_time) < 6` over all (cooling_hours=0 so cooling is not enforced). Local all 131 matches <1 h [L, test data].
**S10. 0-day share**: allocation date minus `COALESCE(harvest_date, DATE(date_added))` = 0 over allocated stems.

## B.7 Manual cleanup today, who does it, how users notice
- **Sales Allocation page "Unallocate (N)"** per bucket per line (SA js:2181, 2441) -> `unallocate_bucket_from_opl`. Any user with the page (no role check in the function). Works only for buckets the page lists: **not for ghosts, held buckets, buckets older than max age, or issued lines.**
- `clearOplAllocations` API (QM:16, v15 Server Script port): whole-order wipe incl. other orders' shares of the same bucket; used to repair "mixed-box bug" damage. Role: any logged-in caller with the endpoint.
- **Shelf Clearer role** Server Scripts `getShelfItemsForClearing` / `clearShelfItem`: removes shelf rows (creates ghosts, does not release BAS).
- Quality-app Discard list (`createDiscardEntry`) blocks allocated buckets; Quality Manager/farm staff see `bucket_allocated` on scan.
- Replace requested bucket (page/app) re-allocates and cancels the old row.
- No "release stale allocations" page, no report, no scheduled sweep, no notification. Users notice only through: "Over-allocation ... 0 available", buckets missing from the allocation page, a line that cannot be substituted, a discard that is refused, or core.stock `over_allocated` / `allocated_in_held` columns (Stock Visibility alert). Owner to call: allocation planner / Sales Manager (unallocate), Stock Manager or Shelf Clearer (ghost shelf), Quality Manager (discard hold), Packhouse/Remote-transfer controller for drafts awaiting transfer.

---
# 7. Dashboard metrics that follow
| Metric | Definition | Source | Call (owner) |
|---|---|---|---|
| Allocation lateness | % OPLs first created at/after 12:00 on delivery-1; median hours from delivery-1 00:00; lines still unallocated at 12:00 | `Order Pick List.creation`, `Sales Order.delivery_date`, Error Log "Incoming Allocation Request" | allocation planners / Sales Manager |
| Confirmation lag | lines open (no confirmed stems) at 09:00 on delivery-1 | `Confirmed Stems`, Version of SO | farm planners |
| Short-allocation backlog by variety/length + reason class | `ordered - SUM(BA cancelled=0)` per line, grouped by (variety, length, region), classed by the A6 waterfall (a empty, b not shelved, c held/old, d wrong length, e other farm, f allocated elsewhere/stale) | A6 | a: harvest/farm manager; b: shelving/quality; c: Quality Manager; d: Sales (downgrade/substitute); e/transfer: transfer controller; f: Stock Manager |
| Remote-only coverage | allocatable stems that exist only on non-sales farms (17,605 of 45,126 locally) | `bucket_rows` by `farm_config.sales_shelf` | transfer controller |
| 0-day-age allocation share | A4 | | harvest manager (cold chain) |
| Cooling-bypass share | A4 / S9 (cooling_hours=0) | | production manager (policy) |
| Stale-allocation stock held (stems, age) | S1 by class, variety, length, farm; plus ghost (S2) and settled-not-released (S3) counts | | Stock Manager / allocation planners (unallocate), Shelf Clearer |
| Reservation drift | S4 | | Stock Manager |
| Held-but-undiscardable | S5 | | Quality Manager |
| Shelving-board inflation | S6 | | shelving supervisor |
| Draft OPL age and reason | draft since > N h: awaiting transfer vs short allocation vs mix group incomplete (`opl_submit_blockers` reasons) | doc 03 | allocator / transfer controller |

# 8. Caveats and uncertain items
- Local is a frozen, partly test-driven snapshot (JTEST/KPKFLOW/SIM-BUCKET ids, BAS only 285 rows, pick rows missing for OPLs created 10-11..10-13). Counts prove the SQL runs, not production magnitudes. Local Shelf Locations have max_allocation_age=4 (production: 5, Simotwo 2); the taxonomy used production values.
- **[?] Whether production clears BAS nightly** (comment at QM:3858); no code or local Server Script does. Settle with S7 first, because it decides whether B.3 is a standing problem or a daily one.
- **[?]** The length-mismatch leak at issue (B.2) is derived from code (MOB:5230-5233 vs SA:3467-3469); one local row (OPL-2026-00043, pick 62cm vs BAS 72cm, issued, BA marked issued) shows the settle can still succeed by other means, so confirm with the production S3 count split by `pick.stem_length <> bas.stem_length`.
- **[?]** Settle exception swallowing: no Error Log "Issue: settling Bucket Allocation Status failed" locally; check production Error Log.
- `Shelf Item.date_added` re-stamp on moves and the age-anchor differences (allocator: Shelf Item.harvest_date/date_added; nightly discard: latest Harvesting SE; discard scan: Receiving SE posting_date) can disagree by a day for reused buckets.
- Quarantine release/shelving is by Server Script and stock entries; its timing was not measured here (see `fetch_quarantine_dwell` Server Script).
- `frappe.throw` errors (Over-allocation) leave no Error Log; to count them add telemetry or read web logs.
