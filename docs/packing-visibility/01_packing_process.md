# 01 - How packing works end to end (research for the "Packing Visibility" v2 page)

Scope: read-only study of code in `apps/upande_packhouse/upande_packhouse` plus local site `kaitet.local` (a thin copy: 1,196 OPLs, only 17 Farm Pack Lists, 44 Box Labels, 6,065 Pick List Items). Production semantics in sections 1-2 (FPL.creation / .modified, bulk-entry vs box-by-box teams) come from the coordinator's production analysis and are NOT reproducible locally; they are marked [PROD]. Anything I could not confirm is marked [UNCERTAIN].
Related existing audit: `docs/backend-v2-audit/workflow.md` (WF-1..WF-16 findings on the v1 API).
Paths below are relative to `apps/upande_packhouse/upande_packhouse/` unless stated. `api.py` = `mobile/api.py`.

---------------------------------------------------------------------------------------------------

## 1. Packing lifecycle, step by step

Chain: Sales Order -> confirm stems -> allocate (OPL + Packing Guide) -> [remote transfer] -> OPL submitted -> ISSUE (scan bucket) -> PACK (scan bunches / enter bunches into a box) -> Farm Pack List (FPL) auto-submit -> Box Labels created -> PRECOOLING in -> STAGED -> LOADED -> DISPATCH (Delivery Note) -> Sales Invoice.

| # | Step | Who / role | Code | Record + fields | Timestamp available | NOT stamped / caveat |
|---|------|-----------|------|-----------------|---------------------|----------------------|
| 0 | Sales Order line planned (boxes, packrate, spec) | Order admin / import | `sales_order_engine.py` (`_set_order_summary`, `_line_packrate`), `spec_autofill.py` | `Sales Order Item.custom_number_of_boxes, custom_packrate, custom_line (spec), custom_mix_group, custom_bunch_group, custom_mixed_box/_bunch, custom_consumables_charge`; `Sales Order.delivery_date` | SO.creation, SO.delivery_date (= the packing "due" date, packing works a day ahead: `api.py:6394-6396`) | `Sales Order.custom_total_boxes` and `SOI.custom_opl` are stale/unreliable (audit WF-1, WF-12) |
| 1 | Allocation creates the Order Pick List (OPL) and its Packing Guide; team assigned | Allocator on the Sales Allocation desk page | `upande_packhouse/page/sales_allocation/sales_allocation.py` (team write `:1901`, `opl_submit_blockers :2245`); `server_scripts/create_straight_box_pick_list.py`, `create_mixed_box_picklist.py`, `create_mixed_bunch_picklist.py`; `packing_guide.py:sync_packing_guide :87` | `Order Pick List` (submittable): `sales_order, team (Link Packing Teams), farm, date_created, order_name, schedule_number, docstatus`; child `table_ytkc` = `Pick List Item` (bucket, item_code, stem_length, stock_qty, issued, shelf, farm...); child `table_nade` = `Packing Guide` (box_number, variety, stems, bunches, pack_rate, box_kind, sales_order_item) | `OPL.creation`, `OPL.date_created` (Date only, NULL on some drafts), PLI.creation | Team is written once ("first allocation wins", `:1895-1901`, `update_modified=False`); no "assigned at" stamp. `OPL.custom_total_stems` is a Data field and goes stale (audit WF-3) |
| 1b | OPL waits as draft (docstatus 0) until all of its stock is allocated / has arrived (remote transfer) | System | `opl_submit_blockers`; `api.py:_submit_for_packing :2100`; hooks `Order Pick List.after_insert/on_update` -> `auto_transfer` | `Order Pick List.docstatus` 0 -> 1 | `OPL.modified` (docstatus flip) - no dedicated "submitted at" field except Frappe's `modified`/Version | Local counts: 77 draft, 1,119 submitted, 0 cancelled |
| 2 | Packhouse Schedule: per team, per day running order | Packhouse supervisor / auto rank | `doctype/packhouse_schedule`, `packhouse_schedule_order`; `api.py:_schedule_blockers :3266`, `rank_schedules` cron (hooks `*/5`) | `Packhouse Schedule(schedule_date, team)` -> `orders[order_pick_list, sequence]` | schedule_date | Enforcement: issue/pack of order #N is refused until earlier orders of the same team are done (`_throw_if_pack_blocked :982`, `PACK_DONE_SQL ~:3237`), unless Production Settings `allow_issue_out_of_sequence` / `allow_pack_out_of_sequence`. Local data: 1 schedule, 2 orders only |
| 3 | ISSUE: packer/issuer scans the bucket at the cold store / shelf, bucket "issued to" the sale-order line | Post-harvest issuer ("Rosenta Issuer", Packer, Line Supervisor). Mobile app "Issuing" | `api.py:issueBucketToSaleOrderItem :4878` (flag set `:5096`; BAS settle `:5227`; stock hop Graded Sold -> Packhouse `post_issue_to_packhouse :5297`; then `_submit_for_packing :5316`); offline variant `api/offline_issue.py`; "pack while waiting" `server_scripts/pack_while_waiting_replacement.py` | `Pick List Item.issued=1` (`issued_offline=1` for Issue Offline); `Bucket Allocations.issued`; stock entry Graded Sold -> Packhouse | `Pick List Item.issued_at` + `issued_by`, BUT these are written afterwards by a 5-minute scheduler (`api/remote_transfer/transfer_steps.py:reconcile :102`, `run :141`, hooks cron `*/5`), at the row's `modified`/`modified_by`. So issued_at is accurate to ~5 min and `issued_by` = last editor of the row. Fallback `COALESCE(issued_at, modified)` is used by v2 (`orders_workflow.py:_first_issue :217`) | `issued` flag alone (`db.set_value`) has no time of its own; rows issued before `issued_at` existed have none. On the local site `issued_at` exists only 2026-10-02..10-11 |
| 3b | Short / not found handling | Issuer + supervisor | `api.py:completeOplShort :2140`; `pack_issued_now` set by server script; `_issued_cap_error :3433` | `PLI.not_found/not_found_at/not_found_by`; `OPL.short_accepted, short_stems, pack_issued_now` | `not_found_at` | Packing is capped at stems issued when an order was sent to packing early (pack-while-waiting, Production Settings `pack_waiting_replacement_pct`, default 50) |
| 4 | PACK: packer scans each graded bunch (spray) or enters bunch qty (standard) into a numbered box | Packer on mobile "Packing" screen. Roles on site: Employee designation Packer (22 active at Post Harvest locally), Sleever (26), Labeler-Sleever | `api.py:createOrUpdateFarmPackList :994` | Creates/updates ONE `Farm Pack List` per OPL (lookup by `order_pick_list`, `:1233-1241`, `:1302-1311`): rows `Farm Packlist Item(box_id, item_code, stem_length, bunch_uom, bunch_qty, stock_qty, under_pack_reason, source_warehouse)`. Spray bunches: `Stock Entry.custom_scanned_packing=1`, `custom_opl_scanned` (`:1366-1375`) | `Farm Pack List.creation` = first scan of the OPL (draft inserted `:1442`); every later scan does `doc.save()` (`:1364`) so `FPL.modified` moves with each scan; `FPL.owner` = user who FIRST scanned; `modified_by` = last scanner. Child row `creation` = when that (box, variety, length, uom) row was first added [UNCERTAIN: verify on prod, it is the only in-progress pack time per row] | No per-scan event/log table, no per-bunch pack time except `Stock Entry.custom_scanned_packing` (bool, no time besides SE.modified). `Farm Packlist Item` has no packer column (the app's own docstring `api.py:6404-6411` says "owner" is the packer). Rows with the same key are MERGED (`:1329-1346`), so history is lost |
| 4b | Packing exceptions | Packer / supervisor | `createPackingBypass :1622` (bunch unscannable); `setPackListBoxUnderPackReason :1505` (close box short); `api/packing_reject.py:record_reject :261`, `approve_replacement :395`; `api/packing_quality.py:report_packing_quality_issue :319` | `Packing Bypass Log(order_pick_list, box_id, reason, bunches, packed_by, farm, greenhouses/harvesters/supervisors)`; `Farm Packlist Item.under_pack_reason`; `Packing Reject Log(status "Pending Approval", stems_rejected, approved_by/at)`; `Bucket Replacement` | creation of each | Local: 4 bypass logs, 0 rejects, 8 Under Pack Reason masters |
| 5 | FPL auto-submit when the Packing Guide is satisfied | System (runs inside the pack call) | `farm_pack_list.py:sync_and_maybe_submit_fpl :172`, `fpl_pack_blockers :89`; hook `Farm Pack List.on_submit -> farm_pack_list_on_submit` | `Farm Pack List.docstatus` 0 -> 1; header carry-forward `sync_header_fields :48` (total_stems, picked_total_stems, consignee...) | `FPL.modified` at submit = the moment ALL boxes of the OPL are complete (or under-pack-reasoned) | Submission is per OPL, not per box: boxes finished early stay in a draft FPL until the last box is done. A Desk "Submit" can also submit an incomplete FPL (still creates labels) |
| 6 | Box Labels created, one per box_number | System, same transaction as submit | `box_label.py:sync_box_labels_for_fpl :57` | `Box Label(order_pick_list, box_number, box_total_count, farm_pack_lis, team, variety, length, pack_rate=packed stems, customer_purchase_order = SALES ORDER name, staged, loaded, delivered, precooling, precooled, precooling_in_at, precooled_at, staging_location, delivery_note)` + `Box Label Item` | `Box Label.creation` = FPL submit time (all labels of an OPL within the same second) | "Box Label printed" has no print timestamp: creation is the best proxy. `Label Print` doctype exists [UNCERTAIN whether it logs print time]. Labels of boxes already in precooling/staged/loaded are never rewritten (`:80-86`) |
| 7 | PRECOOLING in (scan label into room) | Cold-room attendant (13 at Post Harvest) | `api.py:createPrecoolingEntry :1729`; summary `fetchPrecoolingSummary :1776` | `Box Label.precooling=1`, `precooling_in_at` | `precooling_in_at` | "precooled" (out) is only set by the staging scan |
| 8 | STAGED (scan staging location then box) | Dispatch cold-store picker | `api.py:createStagingEntry :1811`; stock hop `post_stage_to_dispatch` | `Box Label.staged=1`, `precooled=1`, `precooled_at`, `staging_location` | `precooled_at` IS the staging time (there is no `staged_at` column); v2 uses `COALESCE(precooled_at, modified)` (`orders_workflow.py:_staged_times :233`) | Requires precooling first (`:1825`) |
| 9 | LOADED onto truck | Loader / dispatch supervisor, "Loading" screen | `api.py:createLoadingEntry :386`; `Loading Plan` (`LP-{date}-{location}-{vehicle}`) | `Box Label.loaded=1`; `Loading Sheet(LS-{date})` -> `Loading Sheet Item(box_label, position, temperature, vehicle)` | `Loading Sheet Item.creation` (v2: `MIN(lsi.creation)` per box, `orders_workflow.py:_flow_times :298`) | Requires staged (`:~430`); Box Label has no `loaded_at` |
| 10 | DISPATCH: Delivery Note built per customer, sheet marked Departed with seal | Dispatch supervisor | `api.py:createOrUpdateDispatch :576` (DN draft insert, `box.delivery_note` set `~:911`); `roses_invoice.py:delivery_note_on_submit :72` auto-creates the Sales Invoice | `Delivery Note(docstatus, posting_date/time)`, `Box Label.delivery_note`, `Loading Sheet.status/seal_number/location_departures (JSON with "at","by")` | DN posting_date+posting_time (v2 `dispatch_at`) | `Box Label.delivered` is never written by any Python in this app (grep: no setter) [UNCERTAIN, may be set by Desk/other app]; v2 uses it only as a cumulative flag. Locally 0 delivered |

### 1.1 What the timestamps really mean [PROD semantics, coordinator analysis + code]
* `Farm Pack List.creation` = pack list opened (first scan row posted). `Farm Pack List.modified` = last save; once complete it equals the submit time = the moment Box Labels are generated (`farm_pack_list.py:farm_pack_list_on_submit`). `Box Label.creation` ~ FPL.modified.
* **Bulk-entry teams (Team A, Team B, Jamafa)**: the packers pack physically first, then a data-entry person keys the whole list (many boxes) and submits within about a minute. FPL.creation/modified therefore shows data-entry time, not packing time. Throughput per hour from these timestamps is meaningless for these teams; a late submit batch (e.g. after 17:00) may reflect typing, not packing.
* **Spray teams (Eldama A, Eldama B, Bravo)**: scan bunch by bunch, box by box; the draft FPL is open for the duration of the OPL and submits when its last box is complete. Duration creation -> modified is a real "pack time of the order". Boxes inside one OPL still all get labels at the final submit instant, so per-box packing time is not available from Box Label.creation; use child-row `creation` [UNCERTAIN] or `Stock Entry.modified` of the scanned bunch (spray only).
* Consequence for the CEO page: use timestamps only for (a) how long something has been waiting in a state (open draft, label without staging), and (b) staged/loaded times (scan-based, reliable for all teams). Do not show "boxes packed per hour" per team as an efficiency measure.
* `issued_at`: see step 3 (~5 min lag, user = last editor).
* Team names on production OPLs (local copy): Team A, Team B, Jamafa, Bravo, Eldama (old), Eldama A, Eldama B, plus NULL. `Packing Teams` master has only 5 (Bravo, Eldama, Jamafa, Team A, Team B): "Eldama A/B" exist on OPLs but not in the master (local counts: Eldama A 577 submitted OPLs, Eldama B 111, Bravo 111, Team A 114, Team B 88, Jamafa 98, NULL 19). `getFilterOptions` (`orders_workflow.py:882`) unions both for that reason.

---------------------------------------------------------------------------------------------------

## 2. Precise meanings

* **OPL (Order Pick List)**: one per straight-box Sales Order line, or one per mixed-box / mixed-bunch group (`pipeline.py` docstring; `boxes.py:17-22`). Unit of issuing and packing. Live = `docstatus < 2`; packable = `docstatus = 1`.
* **Planned box** = distinct `Packing Guide.box_number` on the OPL (`pipeline.py:303-318`, `planned_box_count :404`). Planned stems = `SUM(Packing Guide.stems)`. This is the plan the packer must meet; it can exceed picked stems while stock is short.
* **Issued stems** = `SUM(Pick List Item.stock_qty)` where `issued=1 OR issued_offline=1` (`pipeline.py:283-301`; v1 uses `issued==1` only, `dashboard.py:184`). Bucket-level: a bucket counts issued when all its rows are (`api.py:_issue_progress :3318`).
* **Packed stems** = `SUM(Farm Packlist Item.stock_qty)` over FPLs with `docstatus < 2` (drafts INCLUDED) (`pipeline.py:338-358`).
* **Packed box (v2 "complete box")**: a box_id in which every Packing Guide `(box, variety)` has packed stems >= planned stems, OR the box carries an `under_pack_reason` (closed short on purpose) (`pipeline.py:360-368`). Not the same as "a box_id that appears in the FPL" (v1) nor "has a Box Label".
* **Open box**: a box whose FPL is still a draft: box_id exists in FPL rows (or is in the guide with no rows at all) and the FPL has not been submitted; or a complete box whose siblings are not complete (FPL stays draft until every box is done, `fpl_pack_blockers :89`). After submit a box is "labelled" and is tracked by its Box Label flags.
* **Under-packed box**: `Farm Packlist Item.under_pack_reason` set (via `setPackListBoxUnderPackReason :1505`, stored on every row of that box). Counts as complete in v2; surfaced as an "issue" in both dashboards.
* **Staged (v2)** is cumulative: `staged OR loaded OR delivered`; **precooled** = `precooling OR precooled OR staged...`; **loaded** = `loaded OR delivered` (`pipeline.py:382-386`). A box counts toward a stage once it has reached it or later.
* **Dispatched** = submitted, non-return Delivery Note Items by `so_detail` (`pipeline.py:_attach_dispatch :410`).
* **Stage word rule** (`pipeline.py:439-468`): a stage is "Done" word only when its stems >= total - 0.5 (`STAGE_TOLERANCE :448`), otherwise the "-ing" word when >0, "Not X" at 0.
* **completion_pct** (`pipeline.py:505`): staged stems / ordered stems, 0-100 (None when nothing ordered); the only definition of "Complete" in v2.

---------------------------------------------------------------------------------------------------

## 3. How a box stays "open", and the exact SQL to detect it

Ways a box hangs (each state, where it lives, the clock to use). All SQL below was executed read-only on `kaitet.local` and returns rows (local data is Sept-Oct, so waits are in the tens of thousands of minutes; on prod use a delivery-date window).

| State | Meaning | Clock | Notes |
|---|---|---|---|
| S0 OPL draft | allocated but not submitted (waiting for transfer / full allocation) | `OPL.modified` | cannot be issued/packed until submitted (or pack-while-waiting) |
| S1 submitted, not issued | `OPL.docstatus=1`, PLI `issued=0` | OPL.modified | `orders_workflow._flags` code `not_issued` |
| S2 issued stems idle | issued stems > packed stems (no/partial pack) | `GREATEST(last issue, last FPL.modified)` | v2 flag `not_packing` ("Issued · not packing") |
| S3 draft FPL open | packing started, FPL not submitted (some box incomplete or none ever started) | `FPL.creation` (open time), `FPL.modified` (idle since last scan) | bulk-entry teams: draft is open only for seconds; spray teams: hours |
| S4 planned box never started | Packing Guide box without any FPL row | none (no row) | cannot be aged by itself, age the OPL (S2) |
| S5 label printed, not in precooling/staged | `Box Label` exists, `precooling=0 AND staged=0` | `Box Label.creation` | the real "boxes sitting in the packhouse" |
| S6 in precooling not staged | `precooling=1, staged=0` | `precooling_in_at` | |
| S7 staged, not loaded | `staged=1, loaded=0` | `precooled_at` (= staging time) | |
| S8 loaded, not dispatched | `loaded=1`, no submitted DN | `MIN(Loading Sheet Item.creation)` | DN may exist as draft (`createOrUpdateDispatch` inserts draft) |

Test rows (names starting TEST-, or Box Label with no `farm_pack_lis`) must be excluded (`pipeline.py:376` requires `farm_pack_lis != ''`; locally 10 of 44 labels have none).

```sql
-- S3 draft pack lists still open (per OPL; boxes started vs planned)
SELECT fpl.name, fpl.order_pick_list, opl.team, so.delivery_date,
       TIMESTAMPDIFF(MINUTE, fpl.creation, NOW()) AS open_min,
       TIMESTAMPDIFF(MINUTE, fpl.modified, NOW()) AS idle_min,
       COUNT(DISTINCT fpi.box_id) AS boxes_started, SUM(fpi.stock_qty) AS stems_packed
FROM `tabFarm Pack List` fpl
JOIN `tabOrder Pick List` opl ON opl.name = fpl.order_pick_list AND opl.docstatus = 1
JOIN `tabSales Order` so ON so.name = opl.sales_order
LEFT JOIN `tabFarm Packlist Item` fpi ON fpi.parent = fpl.name
WHERE fpl.docstatus = 0
GROUP BY fpl.name, fpl.order_pick_list, opl.team, so.delivery_date, fpl.creation, fpl.modified;

-- S2 issued stems idle (issued but not packed)
SELECT o.name, o.team, so.delivery_date, i.issued_stems, IFNULL(p.packed_stems,0) packed_stems,
       i.issued_stems - IFNULL(p.packed_stems,0) AS issued_idle_stems,
       TIMESTAMPDIFF(MINUTE, GREATEST(i.last_issue, IFNULL(p.last_pack,'1970-01-01')), NOW()) AS idle_min
FROM `tabOrder Pick List` o
JOIN `tabSales Order` so ON so.name = o.sales_order
JOIN (SELECT parent, SUM(stock_qty) issued_stems, MAX(COALESCE(issued_at, modified)) last_issue
      FROM `tabPick List Item` WHERE (issued=1 OR issued_offline=1) GROUP BY parent) i ON i.parent = o.name
LEFT JOIN (SELECT f.order_pick_list, SUM(fi.stock_qty) packed_stems, MAX(f.modified) last_pack
           FROM `tabFarm Pack List` f JOIN `tabFarm Packlist Item` fi ON fi.parent=f.name
           WHERE f.docstatus<2 GROUP BY f.order_pick_list) p ON p.order_pick_list = o.name
WHERE o.docstatus=1 AND i.issued_stems - IFNULL(p.packed_stems,0) > 0.5;

-- S5 labels waiting to enter precooling
SELECT bl.name, bl.order_pick_list, bl.team, TIMESTAMPDIFF(MINUTE, bl.creation, NOW()) AS wait_min
FROM `tabBox Label` bl
WHERE IFNULL(bl.farm_pack_lis,'')!='' AND IFNULL(bl.precooling,0)=0 AND IFNULL(bl.staged,0)=0
  AND IFNULL(bl.loaded,0)=0 AND IFNULL(bl.delivered,0)=0;
-- S6: same table, precooling=1 AND IFNULL(staged,0)=0, age from precooling_in_at

-- S7 staged, not loaded (age from precooled_at)
SELECT bl.name, TIMESTAMPDIFF(MINUTE, COALESCE(bl.precooled_at, bl.modified), NOW()) AS staged_wait_min
FROM `tabBox Label` bl WHERE bl.staged=1 AND IFNULL(bl.loaded,0)=0 AND IFNULL(bl.farm_pack_lis,'')!='';

-- S8 loaded, no submitted delivery note
SELECT bl.name, bl.delivery_note, (SELECT MIN(lsi.creation) FROM `tabLoading Sheet Item` lsi WHERE lsi.box_label=bl.name) load_at
FROM `tabBox Label` bl
WHERE bl.loaded=1 AND (IFNULL(bl.delivery_note,'')='' OR NOT EXISTS
      (SELECT 1 FROM `tabDelivery Note` dn WHERE dn.name=bl.delivery_note AND dn.docstatus=1));

-- S4 planned boxes with no pack row (join Int box_number to Data box_id works in MariaDB)
SELECT pg.parent, COUNT(DISTINCT pg.box_number) planned, COUNT(DISTINCT fp.box_id) started
FROM `tabPacking Guide` pg JOIN `tabOrder Pick List` o ON o.name=pg.parent AND o.docstatus=1
LEFT JOIN (SELECT f.order_pick_list, fi.box_id FROM `tabFarm Pack List` f
           JOIN `tabFarm Packlist Item` fi ON fi.parent=f.name WHERE f.docstatus<2) fp
       ON fp.order_pick_list=pg.parent AND fp.box_id=pg.box_number
WHERE pg.parenttype='Order Pick List' GROUP BY pg.parent HAVING started < planned;
```
Join keys to remember: FPL<->OPL `Farm Pack List.order_pick_list`; item<->FPL `Farm Packlist Item.parent`; label<->OPL `Box Label.order_pick_list`; label<->FPL `Box Label.farm_pack_lis` (typo is the real fieldname); label<->SO `Box Label.customer_purchase_order` (holds the SALES ORDER name, `box_label.py:~140`, `fetchPrecoolingSummary :1796`); guide<->line `Packing Guide.sales_order_item`; PLI<->line `Pick List Item.sales_order_item` (older rows `custom_sale_order_item`). Never use `Sales Order Item.custom_opl` (audit WF-1).

Caveats: (1) `Farm Pack List.modified` also moves on `sync_header_fields` saves and Desk edits, so "idle since last scan" can be a few seconds optimistic. (2) Box Label.team is a Link filled from the OPL (field exists; locally NULL on 4 of 44 old labels). (3) A draft FPL for an OPL whose stock is only partly issued (pack-while-waiting, `_issued_cap_error`) is legitimately waiting on issuing, not on the packers: separate "waiting for stock" from "packers idle" using `OPL.pack_issued_now`, PLI `not_found`, `awaiting_transfer/in_transit` (`api.py:_issued_stems :3388`).

---------------------------------------------------------------------------------------------------

## 4. Consumables, specification and packrate checks in packing

What exists (all server-side in the pack call unless stated):

| Check | Where | Recorded | Blocks? |
|---|---|---|---|
| Bunch must be graded (spray with bunch_id) | `api.py:1048-1062` | message only | YES: "This bunch has not been graded in the system" |
| Bunch already packed (`Stock Entry.custom_scanned_packing`) | `:1064-1073`, `:1154` | skipped list returned | YES if all scanned are packed already |
| Stem length: bunch graded length vs order length (`_bunch_packing_target :217`) | `:1081-1100` | none | YES for an upgrade (50cm bunch into a 60cm line); downgrades are packed at the order's length |
| Over-pack: ordered stems per (variety, length) | `:1260-1273` | none | YES "Over-pack blocked ... stems ordered" |
| Over-pack: box capacity vs packrate (`custom_packrate` / `custom_packrate_mixed_box`) | `:1276-1289` | none | YES "box N holds X stems" |
| Issued cap (order sent early; cannot pack more than issued) | `_issued_cap_error :3433`, `:1293-1299`, class `IssuedCapError` | none | YES "Only N% is issued" |
| Schedule order (team's earlier orders must be packed first) | `_throw_if_pack_blocked :982`, `_schedule_blockers :3266` | none | YES unless Production Settings `allow_pack_out_of_sequence` |
| Under-pack reason (close a box short) | `setPackListBoxUnderPackReason :1505`, master `Under Pack Reason` (8 reasons locally: Awaiting transfer from another farm, Bucket exhausted mid-box, End of day / cutoff, Insufficient stock on shelf, Mixed bunch recipe shortfall, Order quantity reduced after allocation, Quality rejection, Wrong stem length available) | `Farm Packlist Item.under_pack_reason` on every row of the box | UNBLOCKS: a reasoned box no longer holds the FPL draft (`farm_pack_list.py:fpl_pack_blockers :89`) |
| Packing bypass (QR unreadable / not graded) | `createPackingBypass :1622`, master `Packing Bypass Reason` | `Packing Bypass Log` (reason, bunches, packed_by, farm, greenhouses/harvesters/supervisors via `_opl_traceability :1578`) | audit only, does not block |
| Quality reject while packing | `api/packing_reject.py:record_reject :261`; `api/packing_quality.py:report_packing_quality_issue :319` | `Packing Reject Log` (status "Pending Approval"), Quality Reporting, Rejects stock hop; supervisor `approve_replacement :395` or `close_without_replacement :480` | The reject waits on a supervisor; replacement bucket must be allocated/issued before the box can be completed (this is a real "blocker, who to call": the approver) |
| Spec / consumables | `Specifications.consumables` (child `Spec Consumable`: consumable_type, item, qty_per_bunch/box); copied to SO lines by `spec_autofill.py:~370-389` (`custom_consumables_charge`); `Pick List Item.custom_flower_food` copied from SO line (`server_scripts/create_mixed_*_picklist.py`) | billing/charge flag on the order line | NO packing gate. `Order Pick List.consumables_requested` (Check) exists but nothing in this app sets or reads it (grep: only the doctype JSON) [UNCERTAIN: may be used by the mobile app via direct doc update]. No sleeve / box / flower-food stock-availability check in the pack call. No spec-mismatch approval workflow in packing |

Conclusion: consumable shortages cannot be detected from data today; the only consumable-like signal is `consumables_requested` (0 rows set locally). Spec compliance is enforced up-front at order/allocation time; at pack time only length/packrate/ordered-qty/issued-cap gates exist, and each failure is an app error with NO log row (only `frappe.log_error` "Farm Pack List Packing Error", `:1489`; the Error Log doctype could be mined for "Over-pack blocked", "Not issued yet", "Packing order" counts per user - [UNCERTAIN] volume).

---------------------------------------------------------------------------------------------------

## 5. Team model: who is supposed to pack vs who did

Data model
* `Packing Teams` (autoname by `team`, 5 records locally: Bravo, Eldama, Jamafa, Team A, Team B). It carries ONLY a name: no members, no lead, no location, no roster.
* `Order Pick List.team` (Link, set at allocation), `Pick List Item.team`, `Box Label.team`, `Packhouse Schedule.team` (per day).
* No "Packing Line" doctype exists in the app (`ls doctype`): confirmed absent. Teams ARE the lines.
* Team -> people link: NONE in data. `Employee` has no team field (checked `show columns like '%team%'`). People are inferred only from `Farm Pack List.owner` / `Packing Bypass Log.packed_by` (the session user who scanned; `api.py:6404-6411` says so) and `Box Label.owner`.
* Staff lists: `api.py:getPostHarvestStaff :3219` = Employees with `custom_farm='Post Harvest'`, designation Grader / Packer, status Active. Locally 225 Post Harvest employees: Packer 22, Sleever 26, Grader 107, Labeler-Sleever 2, Cold Room Attendant 13, Line Supervisor 2, Rosenta Issuer 1, Post Harvest Supervisor 2, Print Room Operator 1, Dispatch/Cool-Chain Supervisor 1, Logistic Supervisor 1, Quality Controller 16... Only 11 of 50 packer/sleever employees have `user_id`, so mapping app user -> Employee is incomplete.
* HRMS data available locally (volumes): Employee 2,605; Attendance 267,236 (2026-05-01 .. 2026-09-29); Shift Assignment 3,299 (2025-05-01 .. 2026-09-28); Employee Checkin 71,580 (last 2026-09-04). For designation Packer at Post Harvest: Attendance only 64 rows in Sept (44 Absent 09-01/02, 20 Present 09-03), zero Shift Assignments since 2026-09-01, zero Attendance after 2026-09-29. So roster/attendance is NOT populated for the packhouse in a usable way locally; [UNCERTAIN] on production (check `tabAttendance` joined to `tabEmployee.custom_farm='Post Harvest' AND designation='Packer'` for the last 14 days before relying on it).
* Packhouse Schedule (the only "who is expected to work what today" data): per team per day an ordered list of OPLs (`Packhouse Schedule Order.sequence`). It says which OPLs a team should pack and in which order, not which people. Local: 1 schedule / 2 orders; production use unknown [UNCERTAIN]. `OPL.schedule_number` (Data) is a second, older ordering.
* "Supposed to be packing" therefore = OPLs for delivery date D assigned to team T (OPL.team, or Packhouse Schedule) that are issued but not packed. "Who actually packed" = distinct `Farm Pack List.owner` / `modified_by` per team per day (bulk-entry teams: the data-entry clerk, not the packers; team size must be a manual setting).
* Who to call for each blocker (derivable from data): not issued -> issuer on shift (PLI.issued_by of that day / "Rosenta Issuer"); waiting transfer -> transfer hub (`awaiting_transfer`, `in_transit`, `transit_truck`, Bucket Request Trip); packing stalled -> team lead (no field; use Packing Teams name, needs a new contact field or Employee mapping); reject pending -> approver (`Packing Reject Log` pending; `_can_approve` role); label not staged -> cold-room attendant; staged not loaded -> logistics/dispatch supervisor.

---------------------------------------------------------------------------------------------------

## 6. v1 vs v2: how numbers are counted

v1 page `www/packhouse-dashboard.html` (labels at `:374-415`), API `api/dashboard.py:getDashboardData :14`. v2 page `www/packhouse-dashboard-v2.html` (route `/packhouse-dashboard`, `www/packhouse_dashboard_v2.py`; KPIs `:235-242`), API `api/v2/orders_workflow.py:get_orders_workflow :541`, engine `api/v2/core/pipeline.py`. (Audit doc `workflow.md` describes the v1 API under the v2 page name; the v2 page has since been rebuilt on `orders_workflow`.)

| Metric | v1 definition (cite) | v2 definition (cite) |
|---|---|---|
| Population | OPLs with `date_created` in range (or today/yesterday/7d), optional `team in`; NO docstatus filter, drafts and cancelled counted (`dashboard.py:30-64`) | Sales Order LINES with `so.delivery_date` in range, `so.docstatus=1`, Roses business unit; one item = line x OPL, lines with no OPL shown as "Not allocated" (`pipeline.fetch_lines :102`, `orders_workflow.py:637-704`); OPLs with `docstatus<2` |
| Date meaning | `OPL.date_created` (NULL ones never appear) | Sales Order `delivery_date` (README rule 4) |
| Total OPLs | `len(opls)` after rose filter (`:572`) | `k.opls` distinct OPLs among in-scope items (`_sum_items :534`) |
| Ready to Issue | count of OPLs with `docstatus==1`, even if fully issued/packed (`:519-521`, audit WF-6) | items whose stage is `to_issue` or `issuing` (`orders_workflow.py:796`) |
| Issuing % | `SUM(PLI.stock_qty where issued=1) / SUM(PLI.stock_qty)` per OPL, global = sums; capped 100 (`:433-437, :544`); `issued_offline` ignored | `issued_stems / picked_stems`, issued = `issued=1 OR issued_offline=1` (`_pairs :179`, `_sum_items :528`) |
| Packing % | `FPL.stock_qty sum / OPL.custom_total_stems` (stale allocated stems), capped 100 (`:441-444`) | `packed_stems / planned_stems` where planned = `SUM(Packing Guide.stems)` (`_sum_items :529`) |
| Expected boxes | SOI `custom_number_of_boxes` of rows with `custom_opl in opls` (stale link), deduped by key spec(custom_line) -> bunch_group -> mix_group (`:214-273`) | Planned boxes = distinct `Packing Guide.box_number` per OPL (`pipeline.py:404`); ordered boxes (separate) = `boxes.order_boxes` max per box_key per order (`boxes.py:44`) |
| Boxes packed | distinct non-empty `box_id` per FPL (docstatus != 2, drafts included), summed over FPLs (`:278-299`) | `complete_boxes`: box where every guide (box, variety) met, or under-pack reasoned (`pipeline.py:360-368`); counted once per OPL |
| Fulfilment rate | `total_pkd / total_exp` of boxes (`:561`); inflated denominator (audit WF-1/2) | no fulfilment %; uses `dispatched_pct = dispatched stems / ordered stems` and `completion_pct = staged / ordered` per item |
| Stages | single-line: Fully/Partially Allocated by docstatus (`:499`) | ordered > allocated > issued > packed > precooling > staged > loaded > dispatched > invoiced (`pipeline.py:45`, `orders_workflow.py:65-82`, `_stage :430`) |
| Packing issues | Bypass rows + under-pack rows listed per OPL (`:310-376`) | counts only in KPI/rows (`_issue_counts :346`), detail in `get_opl_detail :823` |
| Mixed-box dedupe | spec first, else bunch_group if mixed_bunch flag, else mix_group if mixed_box flag; `||spec||line` keyed by OPL (`:241-271`); straight rows add own boxes | `box_key`: mixed lines -> (spec, length) else bunch group else mix group; straight -> the line; MAX within key, SUM across keys, per order (`boxes.py:32-52`) |
| Late/problem flags | none | `_flags :477`: late (past due not packed / not allocated / not dispatched), due_today, due_soon, packed_unissued, not_issued, not_packing, short |
| Takt | none | `_takt :260`: FIRST issue (`MIN(COALESCE(issued_at, modified))`) -> LAST box staged; states done/running/none/invalid; per-box `_flow_times :298` |
| Box Label usage | listing of labels created in range (`:394-400`, creation filter) | staged/loaded from Box Label flags, only labels with `farm_pack_lis` set (`pipeline.py:371-400`) |

Known v1 defects (see audit WF-1..WF-16): stale `custom_opl`, `custom_total_stems`, hard-coded `stems // 10` bunches, two date semantics on one page, drafts counted, issued_offline ignored. Known v2 limits (my observations): (a) a line on several OPLs puts its line-level stems on the first OPL only (`orders_workflow.py:689`); (b) `completion_pct` is per item, no global "% of all orders packed" KPI exists; (c) `ladder` measures packing against planned stems, later stages against ordered stems, so "packed 100%" and "staged 40%" can coexist legitimately; (d) the takt timer cannot be computed for lines packed without an issue scan (reason "No issue scan recorded"); (e) `get_orders_workflow` attaches the pipeline to every line of the range, costly for wide ranges.

### Recommended ONE definition set for Packing Visibility
1. **Cohort**: Sales Order lines with `delivery_date` in the selected day(s) (default today + tomorrow, packing works a day ahead), `docstatus=1`, Roses, not Cancelled/Closed; i.e. `pipeline.fetch_lines`. Never OPL.date_created.
2. **Unit of "100% packed"**: boxes. Denominator = planned boxes from Packing Guide (distinct box_number per live OPL) plus ordered boxes of lines with NO OPL yet (`boxes.order_boxes` over those lines), so "not even allocated" counts as unpacked. Numerator = `complete_boxes` (v2). Secondary stem view: packed_stems / ordered_stems.
3. **Packed** = complete box (all guide varieties met or reasoned short) - NOT "box_id present" and NOT "label printed". Track "labelled" (FPL submitted) separately because that is when the box is physically finished for non-bulk teams; for bulk-entry teams treat label time as data entry.
4. **Issued** = `issued=1 OR issued_offline=1`; **issued idle** = issued stems - packed stems on OPLs with docstatus=1 (SQL S2).
5. **Open box** = any planned box that is not complete, and any complete box whose FPL is still draft; age with the S3/S5-S8 clocks.
6. **Stage ladder** exactly `pipeline.STAGES` with `stage_state` tolerance 0.5 and cumulative flags.
7. **Efficiency** = progress against delivery deadline, not throughput: for each delivery date, % boxes complete vs % of the working day elapsed / vs cutoff time (cutoff is a business input, not in data), plus the count/age of boxes in S2-S8. Do not publish boxes/hour per team (timestamps are data-entry for Team A/B/Jamafa).
8. Count draft OPLs separately ("waiting for allocation/transfer"), never inside the packing denominator.

---------------------------------------------------------------------------------------------------

## 7. Reusable KPIs/functions vs new queries

| Need | Reuse as-is (function, file) | Notes |
|---|---|---|
| Cohort lines + ordered stems/boxes | `pipeline.fetch_lines`, `boxes.order_boxes` / `boxes_by_order` | `core/pipeline.py:102`, `core/boxes.py:44` |
| Per-line pipeline (allocated, picked, issued, planned, packed, precooled, staged, loaded, dispatched) | `pipeline.attach_pipeline` | returns `{opl: facts}` with planned/complete/precooled/staged/loaded boxes (`opl_public :569`) |
| Stage state/word/label, completion % | `pipeline.stage_state`, `stage_word`, `line_stage`, `line_stage_label`, `completion_pct`, `STAGES` | `:439-522` |
| Order roll-up | `pipeline.order_rollup` | `:525` |
| Item stage, flags (late, due_today, not_issued, not_packing, packed_unissued, short) | `orders_workflow._stage`, `_flags`, `_progress`, `_ladder` (private, import or lift to core) | `:415-516` |
| KPI sums (issuing %, packing %, boxes, bad/warn counts) | `orders_workflow._sum_items`; endpoint `get_orders_workflow` kpis | `:519`, `:541` |
| Pick-list issuing/planned per (OPL, line) | `orders_workflow._pairs` | `:179` |
| First issue time, staged times, takt | `_first_issue`, `_staged_times`, `_takt`, `_flow_times` | `:217-343` |
| Packing issues (bypass + under-pack) | `_issue_counts`, `get_opl_detail` | `:346`, `:823` |
| Team/farm filter options, region, rose type | `get_filter_options`, `core/region.farms_for`, `core/rose.rose_sql` | |
| Dispatched stems | `pipeline._attach_dispatch` (via attach_pipeline) | |
| Mobile precooling box counts per SO | `mobile/api.py:fetchPrecoolingSummary :1776` (SQL pattern) | |

Needs NEW queries (not available in any existing endpoint):
* S2 issued-idle stems per OPL with age (SQL above); S3 draft FPL age/idle; S5/S6/S7/S8 box ageing counts and lists (existing code only counts flags, never ages them); S4 planned-never-started boxes.
* Open-box total across all dates (not just the delivery-date cohort) - backlog from earlier days still on the floor.
* Per-team daily scoreboard: boxes complete / planned for the team's cohort, number of distinct scanners (`FPL.owner`), time since last scan (`MAX(FPL.modified)` per team) - "is this team moving now?".
* Expected-vs-active people: Packhouse Schedule per team/day + Attendance (empty locally), designation counts; likely a manual "team size today" setting or a roster doctype.
* Blocker taxonomy with owner to call: transfers waiting (`awaiting_transfer/in_transit` PLI + trips), not found/short (`PLI.not_found`, `OPL.short_accepted`), pending rejects (`Packing Reject Log.status`), schedule-order blocks (`_schedule_blockers` logic), over-pack/issued-cap refusals (Error Log mining [UNCERTAIN]).
* Consumables readiness: no data; would need a new doctype/field or stock check on Spec Consumable items per delivery date.
* Global "% of all orders packed" (boxes complete / total boxes incl. unallocated): composable from `order_rollup` + `order_boxes`, but no endpoint exposes it.
* Indexes: `Farm Pack List.order_pick_list`, `Box Label.order_pick_list`, `Box Label.farm_pack_lis`, `Pick List Item.parent+issued`, `Sales Order.delivery_date`, `Order Pick List.sales_order` are unindexed per the existing audit (workflow.md performance profile); check before adding polling queries.

### Open questions / uncertain
1. Whether child-row `creation` of `Farm Packlist Item` reliably gives in-progress pack times (verify on prod).
2. Whether `Box Label.delivered` is set by anything (no Python setter found) and whether Label Print logs print times.
3. Whether `consumables_requested`, Packhouse Schedule and HR Attendance are populated on production (all ~empty locally).
4. Whether production "Eldama A/B" should be merged into `Packing Teams` master (OPL.team is a Link, yet values not in master exist locally).
5. `issued_at` lag/accuracy (5-min scheduler stamping from row `modified`); rows modified after issue (e.g. shelving) before the reconcile ran may carry a later time.
