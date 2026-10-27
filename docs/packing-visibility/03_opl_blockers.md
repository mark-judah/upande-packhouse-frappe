# Packing Visibility: what blocks an Order Pick List (OPL) from being 100% packed

Read-only research on `kaitet.local` (no writes). Code: `apps/upande_packhouse/upande_packhouse`.
Marker legend: **[V]** verified in code and local data, **[C]** from code only, **[?]** uncertain.

## 0. Local data caveats (read before trusting any count)

- The local DB is a frozen snapshot: orders to 2026-10-14, plus JTEST-* test buckets and OPL-2026-00061..66 written 2026-10-23. Exclude `bucket LIKE 'JTEST-%'` and those OPLs. "Today" for age rules is therefore meaningless locally; counts use delivery dates 2026-10-10..12 or all-time as stated.
- `Bucket Allocations` has only 305 rows (298 live) against 6,065 Pick List Items; `Confirmed Stems` has 47. Locally "allocated" must be read from Pick List Item (PLI) and Packing Guide. In production the allocator ledger (`Bucket Allocations` / `Bucket Allocation Status`) is the authority, so both are given. **[?]**
- `Farm Pack List` has 17 rows, so every "packed" figure is near zero locally. Packing-side SQL is correct by definition but untested on volume.
- `Pick List Item.issued_at/by` are filled for 2,731 of 3,154 issued rows (the bucket-journey catalog said empty). `loaded_in_trolley_at`, `left_farm_at`, `arrived_at` are still mostly NULL, so ageing falls back to `modified`/`creation`, which is not safe. **[?]**
- `Farm.farm_manager`, `farm_approver`, `unit_manager` are empty on every farm; the people are in per-farm roles `Farm Manager <Farm>` (Has Role). `Vehicle.employee` is NULL for the one dispatch truck. `Packing Teams` has no lead field (only `team`). So "who to call" is by role plus record owner/modified_by, not a named lead.
- No delivery cut-off / truck departure time exists in the packhouse app. Delivery pressure can only use `Sales Order.delivery_date` (a DATE). Hours-to-delivery needs an assumed cut-off time (propose a Production Settings field `packing_cutoff_time`, default 16:00 on the day before). **[?]**

## 1. Lifecycle facts that drive the rules

1. **Draft vs submitted OPL.** An OPL is a draft (`docstatus=0`) until `sales_allocation.opl_submit_blockers(opl)` is empty: it has rows, nothing awaiting/in transit/on trolley-unshelved, and every line (the whole mix/bunch group for mixed OPLs) is fully allocated. `mobile.api._submit_for_packing` also submits a draft after issuing once issued % >= `pack_waiting_replacement_pct` (default 50) or all non-not-found buckets are issued. Packing and issuing lists show only submitted OPLs. A draft OPL is a blocker by definition. **[V]** (13 drafts have rows, 4 with stems moving; 73 drafts have no rows)
2. **Not found.** `Pick List Item.not_found=1` (+`not_found_at`, `not_found_by`) is set by `transfer_scheduling.mark_bucket_not_found` (remote farm), or a `Bucket Replacement` with reason `Missing` is written by `bucket_replacement.record` (issuing). Both auto-create a `Bucket Watch`. Resolution paths: replacement bucket accepted (PLI re-pointed; replacement stays `Open` until the old bucket is found), `completeOplShort` (sets `short_accepted=1`, `short_stems`, submits) or `pack_while_waiting_replacement` (sets `pack_issued_now=1`).
3. **Stage ladder** (`api/v2/core/pipeline.py`): ordered, allocated, issued, packed, precooled, staged, loaded, dispatched. `orders_workflow._flags` already emits delivery flags (`late`, `due_today`, `due_soon`, `late_dispatch`, `packed_unissued`, `not_issued`, `not_packing`, `short`).
4. **Hub model.** `Production Settings.transfer_hub_farm` = Kapkolia (sales shelf). Karen is another sales farm packing its own orders (no transfers; `scheduler._own_packing_farms`). Torongo, Chepsito, Kaptumbo, Simotwo are remote source farms.

## 2. Blocker catalogue (detection, SQL proof, thresholds, owner, resolution)

SQL is SELECT-only and was run on the local DB. Add `AND so.docstatus=1 AND so.business_unit='Roses'` and exclude JTEST in production queries. Stems = `stock_qty`.

### B1. Order line not allocated or short-allocated  **[V]**
- Rule: Sales Order Item `stock_qty` (ordered stems) > allocated stems (`SUM(Bucket Allocations.quantity_allocated)` where `IFNULL(cancelled,0)=0`; locally PLI `SUM(stock_qty)`), and no OPL covering it.
- Existing: `core.pipeline.fetch_lines` + `attach_pipeline` (`allocated_stems`, `outstanding_stems`, `picked_stems`); `api/v2/allocation_planning.get_allocation_planning` (KPIs `uncovered_stems`, `short_lines`, per (variety, length) `cover`); `orders_workflow._stage()` returns `notalloc` / `allocating`.
```sql
SELECT so.delivery_date, COUNT(*) lines, SUM(soi.stock_qty) stems
FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name=soi.parent
WHERE so.docstatus=1 AND so.business_unit='Roses' AND so.delivery_date BETWEEN :from AND :to
  AND NOT EXISTS (SELECT 1 FROM `tabPick List Item` p WHERE p.sales_order_item=soi.name)
  AND NOT EXISTS (SELECT 1 FROM `tabPacking Guide` g WHERE g.sales_order_item=soi.name)
GROUP BY so.delivery_date;
```
  Local: 10-14: 451 lines / 244,102 stems unallocated; 10-13: 344 lines (0 with an OPL); 10-12: 293 lines, only 51 with a pick/guide row. Short allocation = same join with `SUM(p.stock_qty) < soi.stock_qty - 0.5`.
- Threshold: unallocated and delivery <= 2 days = red; later = amber.
- Owner: allocator, role **Assistant Packhouse Manager** (Sales Manager for stock-short lines). Person: OPL `owner` (OPL creator; abiwott@karenroses.com created 572 of 909 OPLs, cjerono 184, antony.koskei 110). A line with no OPL has no record, so call the role.
- Restores flow: allocation creates the OPL (draft until B2 clears) which enters the issue list. Sub-flag: `allocation_planning.cover[].uncovered > 0` means no stock exists (escalate to farm/harvest) versus simply not yet allocated.

### B2. Draft OPL (allocated but cannot submit)  **[V]**
- Rule: `Order Pick List.docstatus=0` with rows; reason from `opl_submit_blockers(opl)` (importable pure function giving "N bucket(s) still in transit", "N line(s) not yet fully allocated (x/y stems)", "has no pick-list rows"). Mixed box / mixed bunch OPLs stay draft until the whole `mix_group` / `bunch_group` is allocated.
```sql
SELECT o.name, so.delivery_date, o.team, COUNT(p.name) rows_, SUM(p.stock_qty) stems,
       SUM(p.awaiting_transfer=1 OR p.loaded_in_trolley=1 OR p.in_transit=1) moving_rows
FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name=o.sales_order
LEFT JOIN `tabPick List Item` p ON p.parent=o.name WHERE o.docstatus=0 GROUP BY o.name HAVING rows_>0;
```
  Local: 13 drafts with rows (6,179 stems; 4 with moving stems; 9 with neither moving nor not_found, i.e. incomplete allocation or mix group); 73 empty drafts are junk (`rows_>0` filter).
- Owner/resolve: in transit -> B7/B8 owners; incomplete mix group -> allocator (B1).

### B3. Allocation points at stock that cannot be issued (ghost / held / old / quarantined)  **[V code, ? scale]**
`core/stock.bucket_rows` already returns per (bucket, variety, length): `held`, `allocated`, `allocated_in_held` ("order promised stock queued for discard"), `over_allocated`, `too_old`, `cooling`, `age_days`, `in_discard_request`. They need joining back to OPLs by bucket.
Base set `t_u` = PLI of submitted OPLs with `issued=0, issued_offline=0, not_found=0, bucket<>''`: 2,802 rows / 198,290 stems / 557 OPLs locally (includes historic OPLs, so scale is inflated; restrict to open orders in production).
| Sub-type | Detection (join key `UPPER(bucket)`) | Local rows / stems / OPLs |
|---|---|---|
| B3a ghost: bucket on no shelf | no `Shelf Item` with `stem_qty>0` | 1,295 / 86,542 / 462 **[?]** mostly physically issued before flags were set, Shelf Clearer clean-ups, reuse cleanup |
| B3b bucket on open discard request | `Discard Request Bucket.discarded=0` and request `workflow_state IN ('Pending Approval','Approved')` | 645 / 58,445 / 212 |
| B3c bucket too old | on shelf, age (`COALESCE(harvest_date, DATE(date_added))`) > farm `max_allocation_age` (4 days, all farms) or >= `discard_age` (5) | 1,506 / 111,738 / 428 at >= 5 days |
| B3d quarantined | `Stock Entry` type `Receiving Quarantined` for the bucket (id stored lower-case) not later released by `Quarantine Accept/Rejects` | 13 / 1,530 / 10 (release check not applied: upper bound) |
| B3e cooling | hours on shelf < farm `cooling_hours` | none locally (column absent on Shelf Locations) |
| B3f bucket allocated to 2+ live OPLs | `GROUP BY UPPER(bucket) HAVING COUNT(DISTINCT parent)>1` | 410 buckets (legitimate if stems fit; compare to shelf stems) |
| B3g over-allocation | `SUM(Bucket Allocation Status.allocated_quantity)` > shelf stems of (bucket, variety) | 139 buckets / 13,910 stems; 121 allocated buckets on no shelf (11,840 stems); 112 allocated buckets on open discard lists (19,624 stems) |
| B3h bucket reused after allocation | `Stock Entry Harvesting` created after the PLI for the same bucket | 1,801 rows (noisy) **[?]**; `Bucket Reuse Anomaly` with live pick row: 6 buckets |
- Threshold: B3b / B3d red immediately (stock promised but unusable); B3c red when delivery <= 1 day, else amber; B3a amber, shown only if the OPL delivers <= 2 days and is unissued.
- Owner: B3b/B3c the Discard Request approver (`Discard Request.approved_by`); B3d **Quality Supervisor / Quality Manager** (user who ran `releaseFromQuarantine`); B3a **Shelf Clearer** (Shelving Log `Shelf Cleared`, `shelved_by`); re-allocation by the allocator. Role check locally: `Quality Supervisor` and `Shelf Clearer` roles exist but 0 users hold them; existing user roles are Quality Manager, Quality Controller, Grading Qc, Coldstore Attendant, Stock Manager (58), Packing Supervisor (129), Assistant Packhouse Manager (44). **[?]** which role actually staffs each task in production.
- Restores flow: replace the bucket (`replaceBucket` / `replace_for_issuing`) or reallocate other stock (B1 flow); for a discard request, reject the bucket's request row or reallocate.

### B4. Bucket marked not found (missing) and unresolved  **[V]**
- Rule: `Pick List Item.not_found=1` on an OPL with `short_accepted=0` (and not `pack_issued_now`). Short stems = `SUM(stock_qty)` of those rows (`mobile.api._short_info` returns `not_found`, `short_stems`, `short_pct`).
```sql
SELECT p.parent, p.farm, p.not_found_by, MIN(p.not_found_at) since, COUNT(DISTINCT p.bucket) buckets, SUM(p.stock_qty) stems
FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name=p.parent AND o.docstatus=1
WHERE p.not_found=1 AND o.short_accepted=0 GROUP BY p.parent, p.farm, p.not_found_by;
```
  Local: 58 rows / 4,480 stems / 23 OPLs, none short-accepted; 57 are > 3 days old (Torongo 43 by chemjorbildad@gmail.com, Simotwo 14, Kaptumbo 1). 24 of them have the same bucket on a shelf again after `not_found_at` (candidate "Reappeared").
- Threshold: age since `not_found_at` > 4 h amber, > 12 h red; red at once when delivery <= 1 day. Show `short_pct`.
- Owner: farm where it went missing: **Farm Manager <farm>** role (Torongo 8 users) via `not_found_by` (the clerk who reported); sales side **Packing Supervisor**.
- Restores flow: (1) bucket found and shelved (Watch turns Reappeared); (2) replacement chosen (`replaceRequestedBucket`) so the PLI is re-pointed and the Replacement stays `Open`; (3) `completeOplShort` accepts the shortfall; (4) `pack_while_waiting_replacement` packs what is issued (needs issued % >= `pack_waiting_replacement_pct`; not set locally).

### B5. Bucket Replacement open  **[V code; local rows are test only]**
- Rule: `Bucket Replacement.status='Open'`; `reason` Missing / Damaged / Wrong variety / Issued offline / Quality issue / Other; carries `order_pick_list`, `farm`, `stems`, `trip`, `vehicle`, `reported_by`, `reported_at`, `replacement_source` (Sales farm / Remote farm).
- SQL: `SELECT name, order_pick_list, farm, stems, reason, reported_by, reported_at FROM tabBucket Replacement WHERE status='Open'`. Local: 2 rows, both JTEST.
- An Open replacement does not by itself stop the OPL (the new bucket already stands in); it is a loss/accountability item. It blocks only while the replacement is still travelling (`replacement_source='Remote farm'` and the new bucket's PLI still awaiting/in transit, which is B7/B8). Owner: `reported_by`, then Stock Manager. Close with `resolveBucketReplacement` (Found/Discarded/Written Off); `mark_found` closes automatically on re-shelving.

### B6. Bucket Watch (watchlist)  **[V]**
- Rule: `Bucket Watch.status IN ('Watching','Reappeared')`; OPL link in `order_pick_list` (comma list). Auto-added for `Not found during issuing` / `Not found during transfer`; `Reappeared` = a physical event newer than `watching_since` (+3 min grace) found by `rt_watchlist.latest_appearances`.
- Local: 53 watches (51 transfer, 1 issuing, 1 manual); 46 tied to an OPL with a not-found row and nothing issued; by farm Torongo 38, Simotwo 13, Kaptumbo 1, Kapkolia 1; oldest 2026-10-03.
- Use: not a separate blocker but the **resolution state of B4** (Watching = still missing; Reappeared = go and collect; Closed - Confirmed Missing = loss, push replacement or short). `get_watchlist()` and `rt_inactive.get_inactive_buckets()` (idle >= `inactive_bucket_days`, default 4; stages In transit / On trolley / Awaiting transfer / On shelf / Allocated / Received not shelved) exist. Owner: `added_by`; farm via `farm`. Closed with `close_watch`.

### B7. Remote buckets awaiting transfer, no truck  **[V]**
- Rule: `PLI.awaiting_transfer=1`, not loaded, not not_found. Age from PLI `creation` (stamps `left_farm_at`/`arrived_at` come from `transfer_steps.stamp`).
```sql
SELECT p.farm, COUNT(DISTINCT p.bucket) buckets, SUM(p.stock_qty) stems, MIN(p.modified) since
FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name=p.parent AND o.docstatus<2
WHERE p.awaiting_transfer=1 GROUP BY p.farm;
```
  Local: Torongo 22 rows / 1,910 stems (OPL-2026-00056 and -00026, drafts untouched since 2026-10-08 and 2026-09-11, i.e. stuck), Simotwo 1 (JTEST).
- Threshold: awaiting > 6 h amber, > 12 h or delivery tomorrow red; `Bucket Request Trip(.Order).unscheduled=1` = no run planned (`bucket_logistics.no_trip_section`).
- Owner: transfer planner **Assistant Packhouse Manager** (`scheduler.rank_schedules`; `auto_transfer.run` every 5 min when `auto_remote_transfer_scheduling=1`, it is 1 locally); truck = `Bucket Request Trip.vehicle` (driver via `Vehicle.employee`, null locally; else trip `requested_by` / `released_by`; route in `Bucket Logistics Route`).
- Restores flow: trip planned and dispatched (B8).

### B8. On trolley / on truck / in transit, trip not dispatched or not arrived  **[C; few local rows]**
- Rule: trolley (`loaded_in_trolley=1`) not on truck; trip `Draft/Requested/Scheduled` with buckets loaded but `dispatched_at IS NULL`; trip `Dispatched` with `received_at IS NULL AND arrived_at IS NULL`. State helper: `rt_logistics._trip_state` (not_left / in_transit / arrived); `bucket_logistics.trip_stages`.
```sql
SELECT name, vehicle, status, trip_date, run, total_buckets, dispatched_at,
       TIMESTAMPDIFF(MINUTE, dispatched_at, NOW()) mins_on_road
FROM `tabBucket Request Trip`
WHERE (status='Dispatched' AND received_at IS NULL AND arrived_at IS NULL)
   OR (status IN ('Draft','Requested','Scheduled') AND trip_date < CURDATE());
```
  Local: 3 JTEST trips only (one Dispatched, never received). The midnight job `close_previous_day_trips` ends every old trip; buckets of trips that never left get `keep_as_anomaly` (trolley/truck flags cleared, NOT re-awaiting), so they carry no flag at all and surface only via `rt_inactive` or B3a. **[?]**
- Threshold: in transit > 2 h (use per-leg median from `Farm Distance`), trolley not on truck > 1 h.
- Owner: truck driver (`Vehicle.employee`), trip `released_by`, **Assistant Packhouse Manager**; farm side Farm Manager <farm>. Restores flow: truck arrives, hub shelving sets `shelved=1`, `end_shelved_trips` (every 5 min) closes the trip.

### B9. Arrived at hub but not shelved / not issued  **[V]**
- Rule: PLI `shelved=1`, `issued=0` on a submitted OPL (ready to issue); or `Bucket Request Trip Bucket.shelved=0` on a Received trip ("not shelved when trip ended").
- Local: 248 rows / 30,488 stems shelved but not issued (historic included). Owner: hub shelving staff (`Shelving Log.shelved_by`, Stock User); fix: shelving scan (`createShelvingEntry`).

### B10. OPL ready to issue but not issued, or partly issued  **[V]**
- Rule: submitted OPL; issued stems = `SUM(stock_qty WHERE issued=1 OR issued_offline=1)`; 0 = "To issue"; 0 < x < picked = "Issuing". Existing: `_flags` `not_issued`, stage `to_issue` / `issuing`; `mobile.api._issue_progress`, `_issued_stems`.
```sql
SELECT o.name, so.delivery_date, o.team, SUM(p.stock_qty) picked,
       SUM(IF(p.issued=1 OR p.issued_offline=1, p.stock_qty, 0)) issued
FROM `tabOrder Pick List` o JOIN `tabSales Order` so ON so.name=o.sales_order
JOIN `tabPick List Item` p ON p.parent=o.name
WHERE o.docstatus=1 AND o.short_accepted=0 GROUP BY o.name HAVING issued < picked-0.5;
```
  Local: nothing issued and no pack list: 302 OPLs / 132,716 stems; partially issued: 254 OPLs / 65,384 unissued stems. Deliveries 10-10..12: 219 OPLs, 141,129 picked, 59,307 issued (42%).
- Threshold: at shift start, any OPL delivering today/tomorrow not 100% issued = amber; red when hours to cut-off < stems / team packrate.
- Owner: `Order Pick List.team` (Eldama A/B, Team A/B, Bravo, Jamafa; no lead field) and the issuer: `Pick List Item.issued_by`, `issued_at`, or Shelving Log `Issued to Sales Order` (2,963 rows) / `Offline Issuing` (3,276). Role **Packing Supervisor** (129 users). Issuing by user/shift: group `issued_by`, `HOUR(issued_at)`. Issue reasons: offline reason is a Comment on the Shelving Log; `PLI.issued_offline=1`; `downgrade_reason` for downgraded length.
- Restores flow: issue scan sets `issued=1`; `_submit_for_packing` submits draft OPLs at >= 50% issued.

### B11. Issued stems idle (issued, not packed)  **[V rule, ? data]**
- Rule: issued stems minus packed stems (`Farm Packlist Item.stock_qty` on `Farm Pack List.docstatus<2`) > 0 and `NOW() - MAX(issued_at)` > threshold. Existing flag `not_packing` (stage `to_pack`); `packed_unissued` is the reverse inconsistency.
- Local: fully issued with no pack list: 324 OPLs / 181,488 stems (no FPL data, untrustworthy). Threshold proposal **[?]**: > 3 h amber, > 6 h red (flowers out of water).
- Owner: packing team, Packing Supervisor. `allow_pack_out_of_sequence=1` and `allow_issue_out_of_sequence=1` locally, so the packing sequence cannot be used as the cause. Restores flow: packing into boxes (Farm Pack List).

### B12. Packing-side blocks  **[V schema; small local counts]**
| Sub-type | Detection | Local |
|---|---|---|
| Missing packing guide | submitted OPL with no `Packing Guide` row | 3 OPLs |
| Spec / guide mismatch | per OPL `SUM(Packing Guide.stems)` (planned) vs `SUM(PLI.stock_qty)` (picked); `attach_pipeline` gives both, `_flags` `short` | compute |
| Consumables requested not delivered | `Order Pick List.consumables_requested=1` | 0 locally; no "delivered" field found in code, so only "requested" is detectable **[?]** |
| Packing bypass | `Packing Bypass Log` (reason Damaged/Missing Bunch QR Code, `box_id`, `bunches`, `packed_by`) | 4, last 2026-09-20 |
| Under-pack | `Farm Packlist Item.under_pack_reason<>''` (box counts complete) | 4 rows / 770 stems |
| Packing reject / replacement keeping length | `Packing Reject Log` (`status`, `donor_bucket`, `stems_rejected`, `stems_replaced`), `Stem Replacement Log`; `api/packing_reject.py` approvers: Packing Supervisor, Assistant Packhouse Manager, System Manager | not measured |
| Box limit / packrate | `Packrate`, box type; line `custom_packrate` / `custom_packrate_mixed_box` | no hard block found, planning input only **[?]** |
| Mixed bunch / mix box | `mix_group` / `bunch_group` OPL waits for the whole group | see B2 |
- Owner: Packing Supervisor; Quality Supervisor/Manager for rejects; Assistant Packhouse Manager for specs. Restores flow: guide fixed, box can close.

### B13. Order-level modifiers (not blockers)  **[V]**
- Delivery pressure: `days = delivery_date - today`; `late` (<0), `due_today`, `due_soon` already in `orders_workflow._flags`. No customer-priority field exists on SO or OPL; `PLI.transfer_priority` (ASAP) is mostly empty. **[?]**
- Team: `Order Pick List.team` blank = "Unassigned" (9 submitted OPLs / 1,820 stems; 18 blank overall); `Packhouse Schedule` (PSCH-date-team, child `Packhouse Schedule Order.sequence`) via `scheduler.getSchedulerFeed` gives each team's order of work; `schedule_number` is set on 1 OPL only (ignore). An unassigned OPL is unpackable; owner Assistant Packhouse Manager via the scheduler page.
- Local open stems by team (10-10..12): Eldama A 29,810, Jamafa 17,740, Team B 15,324, Team A 10,808, Eldama B 6,930, Unassigned 840, Bravo 370.

## 3. Who is responsible for a given record (lookup order)

| Blocker | First field to read | Then | Fallback role |
|---|---|---|---|
| B1 not allocated | none (no record) | last OPL `owner` of the same customer/SO | Assistant Packhouse Manager |
| B2 draft OPL | `Order Pick List.owner` | `modified_by` | Assistant Packhouse Manager |
| B3 stock unusable | B3b `Discard Request.approved_by`/`requested_by`; B3d SE `owner` of `Quarantine*`; B3a Shelving Log `Shelf Cleared` `shelved_by` | PLI `shelf`/`farm` -> role `Farm Manager <farm>` | Quality Manager / Stock Manager |
| B4 not found | `Pick List Item.not_found_by` (+`not_found_at`) | `Bucket Watch.added_by`; role `Farm Manager <farm>` (`PLI.farm`) | Packing Supervisor |
| B5 replacement | `Bucket Replacement.reported_by` | `resolved_by` | Stock Manager |
| B6 watch | `Bucket Watch.added_by` | `farm` | Packing Supervisor |
| B7 awaiting transfer | trip `requested_by` | PLI `farm` -> Farm Manager role | Assistant Packhouse Manager |
| B8 trip / truck | `Bucket Request Trip.released_by`, `vehicle` -> `Vehicle.employee` (driver) | `Bucket Request Trip Bucket.loaded_by`; `Bucket Logistics Route.owner` | Assistant Packhouse Manager |
| B9 arrived not shelved | `PLI.arrived_by` / `shelved_by` | trip `arrived_by` | Stock User lead |
| B10 not issued | `PLI.issued_by` (partial) else `Order Pick List.team` | Packing Supervisor | Packing Supervisor |
| B11 idle issued | `issued_by` of latest issued row; `Order Pick List.team` | Packing Supervisor | Packing Supervisor |
| B12 packing | `Farm Pack List.owner` / `Packing Bypass Log.packed_by` | `Packing Reject Log` approver | Packing Supervisor |
| B13 unassigned | none | `Order Pick List.modified_by` | Assistant Packhouse Manager |

Phone/contact: no phone field was checked on User/Employee (not researched, **[?]**). Resolve a role to people with `Has Role` (`parenttype='User'`) intersected with `User.enabled=1`; for a farm use `Farm Manager <Farm>` roles. There is no per-team lead, so propose a new field `Packing Teams.team_lead` (new work).

## 4. Bucket Journey v2 effects on buckets under an allocated OPL

Source: `docs/bucket-journey/EVENT_CATALOG.md`, `api/v2/rt_journey.py`, `core/bucket_activity.py`.
- A journey event does **not** change the OPL by itself. PLI flags are overwritten state, not history; the only OPL-affecting writes are PLI flags (`not_found`, `awaiting_transfer`, `loaded_in_trolley`, `in_transit`, `shelved`, `issued`, `issued_offline`), PLI re-point on replacement, and BAS/Bucket Allocations cancel.
- **Quarantined** (SE `Receiving Quarantined`, bucket id lower-case) under an allocated OPL: nothing marks the PLI; the stock leaves the sales shelf (warehouse "<Farm> Receiving Quarantined"). The OPL looks allocated but cannot be issued. Detect with B3d (join with `UPPER()`); resolution: `releaseFromQuarantine` (accept N, reject rest, partial release splits qty) then re-check stems vs PLI `stock_qty`.
- **Not found** (PLI flag or Bucket Replacement Missing): PLI keeps the row, awaiting/trolley flags cleared, a Bucket Watch is created; on re-shelving the Watch flips to Reappeared and the Replacement to Found (`mark_found`).
- **Replaced**: the PLI row is re-pointed to the new bucket, old BAS cancelled, old shelf row "Shelf Cleared", the new bucket's shelf row removed with reason "Replaced" (journey reads this backwards as a swap-out). The old bucket's journey loses the order; the new bucket inherits the original PLI `creation`. For visibility, a replaced PLI is healthy (resolved) if the new bucket is on the sales shelf or issued.
- **Reused before discard / before issue**: a fresh harvest of the bucket id deletes the stale Shelf Item and writes `Bucket Reuse Anomaly` (`Issuing Skipped`, `Discarding Skipped`, `Skipped Transfer`) without closing the Shelving Log or touching the PLI; the PLI then points at a bucket whose physical stems are another variety (B3a/B3h). Locally 6 anomaly buckets with live pick rows.
- **Discard list**: nightly auto Discard Request (age >= `discard_age`, 5 days) lists shelved buckets including allocated ones (`allocated_in_held`); `createDiscardEntry` deletes the Shelf Item and flags all request rows discarded, but the Shelving Log stays open and the PLI keeps pointing at it. 645 unissued rows sit on open lists locally.
- **Idle buckets**: `rt_inactive.candidates()` covers an `Allocated` stage (open pick row or live Bucket Allocations line, bucket on no shelf) and `On shelf` allocated flag; activity time comes from `bucket_activity.events` (harvest, grade, receive, quarantine, shelve, move, allocate, issue, transfer step, replacement, discard, count). Caps in the v2 journey (500 PLI rows, 5 transfers, one card per OPL) mean it must not be the source of per-OPL counts.

## 5. Proposed blocker taxonomy (11 categories, ranked by how early they stop the OPL)

| # | Category | Rule (from section 2) | Default owner role | Existing in code |
|---|---|---|---|---|
| 1 | Not allocated | B1 no pick/guide for line | Assistant Packhouse Manager | `allocation_planning.get_allocation_planning`, `pipeline.attach_pipeline`, `orders_workflow._stage` |
| 2 | Short allocation / no stock | B1 allocated < ordered; `cover.uncovered` | Assistant Packhouse Manager / farm | `allocation_planning` cover/`short_lines`; `orders_workflow._flags` `short` |
| 3 | Draft OPL waiting (mix group / incomplete) | B2 | Assistant Packhouse Manager | `opl_submit_blockers` (sales_allocation page), stage `allocating` |
| 4 | Allocated stock unusable | B3 a-h (held, old, quarantined, ghost, over-allocated) | Quality Manager / Shelf Clearer / Stock Manager | `core/stock.bucket_rows` (`held`, `allocated_in_held`, `over_allocated`, `too_old`, `cooling`), `stock_visibility`, `discards.get_discards` |
| 5 | Bucket missing | B4 + B5 + B6 | Farm Manager <farm> / Packing Supervisor | `rt_watchlist.get_watchlist`, `bucket_replacement.open_replacements`, `mobile.api._short_info`, `rt_inactive.get_inactive_buckets` |
| 6 | Awaiting transfer | B7 | Assistant Packhouse Manager | `remote_transfer.get_logistics`, `rt_logistics.get_logistics_view`, `scheduler.getSchedulerFeed` |
| 7 | Trolley / truck / in transit | B8 | Driver / Assistant Packhouse Manager | `rt_logistics._trip_state`, `bucket_logistics.trip_stages`, `get_bucket_detail` |
| 8 | At hub, not shelved | B9 | Stock User lead | `bucket_logistics` stages (`shelved`), trips |
| 9 | Ready to issue, not issued / partly issued | B10 | Packing Supervisor | `orders_workflow._flags` `not_issued`, `mobile.api._issue_progress` |
| 10 | Issued, idle (not packing) | B11 | Packing Supervisor | `_flags` `not_packing` (no time component) |
| 11 | Packing exception (guide, bypass, under-pack, reject, consumables, team unassigned) | B12 + B13 | Packing Supervisor / Assistant Packhouse Manager | `orders_workflow.get_opl_detail` (bypass, under-pack); `packing_reject.py` |

Assignment rule: each OPL gets one **primary blocker** = the earliest category in 1..11 that applies (the flow cannot proceed past it); secondary blockers listed beneath it. Stems blocked = the stems of that OPL that the primary blocker holds up (see below).

## 6. One severity rule (works for one OPL, sums to day/CEO)

```
risk(OPL)   = stems_blocked × urgency
stems_blocked = stems of the OPL not yet at the packed stage because of the primary blocker
  (1,2: ordered - allocated; 3: all picked stems; 4/5: stems on affected rows (`stock_qty`);
   6,7,8: stems on moving/awaiting rows; 9: picked - issued; 10: issued - packed; 11: planned - packed)
urgency     = 1 / max(hours_to_cutoff, 4)          # hours_to_cutoff = (delivery_date - 1 day at packing_cutoff_time) - now; <=0 -> 1/4 ("late")
aged        = × 1.5 if blocker age > its red threshold from section 2
band        = RED if hours_to_cutoff <= expected_pack_hours(OPL) + 4 [expected = stems_blocked / team packrate, fallback 1500 stems/h/team]
              or primary in (4: held/quarantined, 5) AND delivery <= 1 day;  AMBER otherwise; GREEN = none
```
Aggregation: day view = sum of `stems_blocked` by delivery_date x category (and a count of OPLs and RED OPLs); CEO headline = "N stems (X% of the day's ordered) blocked, by category, top 3 owners by `risk`". Owner card = sum of `risk` by owner role/person (section 3). All terms are additive, so one OPL, one team, one farm, one day and the whole page reconcile. Because `hours_to_cutoff` is not stored anywhere today, the cut-off time is the one new setting this needs.

Sanity run (local, delivery 10-10..12, one primary cause per OPL, drafts first, then not_found, moving, ghost, to_issue): ghost allocation 138 OPLs / 69,732 unissued stems **[?]** (inflated by cleared-shelf history), to_issue 24 / 6,280, not_found 11 / 5,810 (primary cause ordering used in this run was simplified, not the final rule). Local proves the queries run; it does not prove magnitudes.

## 7. Already in api/v2 vs new queries

Exists (reuse as-is):
- `core.pipeline.fetch_lines`, `attach_pipeline`, `order_rollup`, `line_stage`, `stage_ladder` (ordered/allocated/issued/packed/staged/loaded/dispatched stems per line and OPL).
- `orders_workflow.get_orders_workflow` (per-item stage and `_flags` incl. late/due_today/not_issued/not_packing/short), `get_opl_detail` (bypass + under-pack).
- `allocation_planning.get_allocation_planning` (uncovered/short lines), `core.stock.bucket_rows/summarize/available_cells` (held/old/cooling/over-allocated), `stock_visibility`, `discards.get_discards`.
- `rt_watchlist.get_watchlist/get_bucket_watch/run_watchlist/latest_appearances`, `rt_inactive.get_inactive_buckets/candidates`, `rt_journey.get_bucket_journeys`, `rt_logistics.get_logistics_view/get_bucket_detail`, `remote_transfer.get_logistics/get_bucket_overview/get_buckets_in_use`, `order_fulfilment.get_order_fulfilment` (fill rate by manager/team).
- v1 helpers reusable server-side: `sales_allocation.opl_submit_blockers`, `mobile.api._short_info/_issue_progress/_issued_stems`, `bucket_replacement.open_replacements`, `scheduler.getSchedulerFeed`, `bucket_logistics.getBucketLogistics`.

Need new queries (nothing found):
1. OPL-level join of B3 conditions (stock-unusable under a live PLI): held/old/quarantined/ghost/over-allocated per OPL, exposing the bucket id (currently only per bucket row).
2. Quarantine "not yet released" test per bucket (latest Receiving Quarantined vs Quarantine Accept/Rejects by `custom_receiving_batch_id`).
3. Per-OPL age of each blocker (not_found_at, awaiting since, issued_at max, trip dispatched_at) with the thresholds above, and `hours_to_cutoff` (new `packing_cutoff_time` setting).
4. Issued-not-packed time (issued_at vs first Farm Pack List creation).
5. Stale / never-dispatched trip list and "anomaly" buckets left by `keep_as_anomaly` (no flag; needs a Bucket Transfer Event stage lookup).
6. Responsible-person resolver (Has Role + enabled user) and a team lead field.
7. Consumables delivered flag (none exists), customer priority (none exists).
8. The single `primary_blocker` classifier and the risk roll-up in section 6.

Performance notes: use grouped queries keyed by OPL name set (as `attach_pipeline` does), not per-OPL loops; `rt_journey.build` is slow (watchlist budget 12 s) and must not be called per OPL.

## 8. Uncertainties to confirm with the user/site (summary)
- Cut-off time / truck departure per delivery date (no field).
- Production roles: which role truly performs shelf-clearing, quarantine release and discard approval (`Shelf Clearer` and `Quality Supervisor` roles had 0 local users).
- Whether Bucket Allocations/BAS are complete in production (local has 305 rows vs 6,065 PLI), which decides B1/B3g sources.
- Whether `issued_at`, `loaded_in_trolley_at`, `left_farm_at`, `arrived_at` are filled in production; they drive ageing of B7-B11.
- Thresholds (hours) are proposals from flower handling logic, not read from settings; only `discard_age` (5), `max_allocation_age` (4), `inactive_bucket_days` (4 default) and `pack_waiting_replacement_pct` (50 default) exist as settings.
