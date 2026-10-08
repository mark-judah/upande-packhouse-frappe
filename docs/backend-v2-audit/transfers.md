# Transfers (TR): Remote Transfers v2: Transfer Scheduling, Scheduler, Truck Routes

Site checked: kaitet.local. The remote-transfer data there is thin. Only 5 Pick List Item (PLI) rows have any transfer flag set. 2 of them are `awaiting_transfer` (OPL-2026-00026 @ Torongo). There are **0 Bucket Request Trips**, 0 routes, 0 route templates, 1 Packhouse Schedule (2026-09-17, Team A) and 1 internal truck (KDT 177A, 4x20 = 80 buckets).

Most trip and claim maths can therefore only be proven from the code. Every finding says whether it was proven on real data or from the code.

How I probed it: I ran a read-only probe in `bench console` that wraps `frappe.db.sql` to count queries. It monkey-patched `transfer_hub` to return "Kapkolia" (see TR-1) and `get_single_value('takt_time')` to return 0 (see TR-2) so the payloads could be computed at all. It made no writes: no templates or dispatched trips exist, so the side-effect write paths in the read code (TR-14) never fired.

---

## Page: Transfer Scheduling (/remote-transfer-v2?tab=transfer): API `api.transfer_control.getTransferScheduleData`

`api/transfer_control.py` is a `sys.modules` alias of `api/remote_transfer/transfer_scheduling.py`, so `getTransferScheduleData` is `_transfer_schedule_payload` (line 1697). The page also calls `saveBucketTrip`, `deleteBucketTrip`, `changeTripVehicle`, `addFarmTrip`, `logDistribution`, `mergeTruckTrips` and `auto_transfer.runNow`. I only analysed the writes, I did not execute them.

### What the page shows and how it's computed (brief)
- **Server** (`_transfer_schedule_payload`):
  - `opl_rows`: every OPL with docstatus < 2 and SO delivery_date in the window (or an ASAP replacement) that has at least one PLI row flagged awaiting / loaded / in_transit / shelved and not "packed" (`custom_ready_for_packing` or `issued`).
  - `_transfer_buckets` collapses PLI rows to one entry per (opl, UPPER(bucket)), summing stems. Farm = `FARM_EXPR`: the first word of source_warehouse, else `pli.farm`.
  - `open` = (awaiting or loaded) and not in_transit and not shelved. `on_road` = in_transit and not shelved.
  - Per order and farm it returns `buckets` / `stems` (open, non-hub) and `on_road`. Per order it returns `trolley_buckets` / `arrived_buckets`.
  - An order with no schedule entry goes to `unscheduled`. Schedule and team come from `_schedule_map` (the latest Packhouse Schedule in the last 14 days, plus a fallback for every teamed OPL).
  - Also returned: vehicles + runs, trips (today's plus up to 7 days of unreceived), routes, `_truck_status`, `hub_shelf_space`, `hub_ready_orders`, `_left_behind`, `_distributions`, `duplicate_trips`.
- **Client** (`_planner_common.html` + `transfer_scheduling.html`):
  - `coverMap` = Σ trip-row `buckets` over active (Draft/Scheduled, not stale) trips per (opl, farm).
  - `openFarms` = farm.buckets − covered, with stems pro-rated as average stems per bucket × open.
  - KPI "Not on a trip" = Σ openTotal. "Scheduled orders" = orders.length. "Trips" = trips whose orders' delivery_date equals the page date. "Hub shelf space" = hub_space.free.
  - Distribute-all (`computeDistribution` / `suggestPlan` / `packPorts`) packs one sequence step at a time into the truck runs' remaining room = cap − max(total_buckets, loaded_buckets).

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| TR-1 | Critical | Integrity | **The whole Remote Transfers planner (Transfer Scheduling and Truck Routes tabs, plus auto-planning) is dead on kaitet.local.** `transfer_hub()` throws when `transfer_hub_farm` is unset and there is more than one sales-shelf farm. Here Kapkolia and Karen are both `sales_shelf=1`. `getTransferScheduleData` returns `{success:false}`, so `PH.call` rejects and the tab shows an error. Truck Routes loads `RTP.load()` inside `Promise.all` (truck_routes.html:66), so it fails too, even though routes and templates don't need the planner feed. | transfer_scheduling.py:76-102. `bench execute …transfer_hub` raised: "Set Remote Transfer Hub Farm in Production Settings". tabSingles has no `transfer_hub_farm`. Shelf Locations: Kapkolia 1/1, Karen 1/1. | Treat the hub as required config that is validated in Production Settings. Show a setup state rather than failing every feed. Truck Routes should not depend on the planner feed (TR-17). |
| TR-2 | Critical | Integrity | **The Scheduler feed always errors**, on every site, because it reads a non-existent field. `frappe.db.get_single_value("Production Settings", "takt_time")`: the field is `custom_takt_time` (the doctype lives in upande_quality). The exception is caught and the page gets `{success:false, error:"Field takt_time does not exist…"}`. The same bug is in mobile/api.py:3158. | scheduler.py:61. Probe: all 5 dates tried returned the error with 1 query. DocField: only `custom_takt_time` exists on Production Settings. | Read `custom_takt_time` (or `.get()` on the cached doc). |
| TR-3 | Critical | Integrity | **The "transfer truck" test contradicts the fleet definition.** The planner fleet = Vehicles with `custom_is_internal_logistics_truck=1`. `TRANSFER_TRUCK_OK`, the mobile load picker (`custom_dispatch_truck=0`), `changeTripVehicle` and `auto_receive_trucks_for_bucket` all require `custom_dispatch_truck=0`. `saveTransferTruck` explicitly creates "both" trucks (internal=1, dispatch=1). On kaitet the **only** planner truck, KDT 177A, is internal=1 AND dispatch=1. Trips can be planned on it, but the mobile app can't pick it for loading. `record_truck_load` drops its loads (TRANSFER_TRUCK_OK is in its WHERE), `_truck_status` never shows it, and auto-arrival can't find it. Meanwhile KCF 700Z and KDP 283L (internal=0, dispatch=0) pass `TRANSFER_TRUCK_OK` but are not in the planner fleet. | transfer_scheduling.py:61-64, 403, 937, 1291, 1609, 2710. truck_routes.py:290. mobile/api.py:3573, 5506. Data: `SELECT name, custom_is_internal_logistics_truck, custom_dispatch_truck FROM tabVehicle` → KDT 177A 1/1, KCF 700Z 0/0, KDP 283L 0/0. | Use one predicate everywhere: transfer truck = `custom_is_internal_logistics_truck=1` (dispatch flag irrelevant). |
| TR-4 | High | Math/KPI | **The client "Not on a trip" and Distribute under-count while a trip is loading.** `coverMap` adds the trip row's full planned `buckets`. The server's `farms[].buckets` already excludes loaded buckets: loading sets `in_transit=1`, so they move to `on_road`. The server's own `_trip_claims` subtracts `loaded_buckets`. Example: an order has 12 at a farm, the trip plans 10, 6 are loaded (trip still Draft, "loading"). Server: open 6, claimed 10−6 = 4, **2 truly unplanned**. Client: 6 − 10 → **0**, so Distribute never plans the 2. Auto-planning has the same bug for human trips (auto_transfer.py:329). | _planner_common.html:263-266, 279. transfer_scheduling.py:357-384 (server uses `buckets − LEAST(loaded,buckets)`), 1291 (loads require in_transit=1). Not provable on data (0 trips). | covered = Σ max(0, buckets − loaded_buckets) over active trips, i.e. the same as `_trip_claims`. Better: return `claimed` per (opl, farm) from the server and stop recomputing on the client. |
| TR-5 | High | Math | **A physical bucket split across orders is counted once per OPL.** Buckets are keyed (opl, bucket), so "Not on a trip", truck room in Distribute, saved trip `buckets`, `total_buckets`/capacity checks and hub "planned" all count it 2-3×. Hub `on_road` (DISTINCT bucket) counts it once, so free space is internally inconsistent. | transfer_scheduling.py:224-282 (key = (opl, bucket)). Data: 11 buckets sit in 2-3 OPLs (e.g. TDVRC7 in 3 OPLs, 04188B in 3, RDHWGJ in 2 with 20 rows). | Truck slots and shelf space are physical: count DISTINCT UPPER(bucket) per farm/trip, and attribute the stems per order. `record_truck_load` already keeps one trip per bucket (`chosen`). |
| TR-6 | High | Filter | **Rows created by `_update_existing_pick_list` are born "packed".** Each row gets `custom_ready_for_packing: 1` even when `awaiting_transfer: 1`. `PACKED_SQL` treats rfp=1 as "at the packhouse", so the remote bucket is invisible to Transfer Scheduling: the OPL is dropped by `AND NOT PACKED_SQL` and `_transfer_buckets` marks it shelved. It is also invisible to claims and auto-planning, while the Scheduler (which ignores rfp) still shows it waiting at the farm. | sales_allocation.py:2449-2450 vs transfer_scheduling.py:70, 222, 242. create_straight_box_pick_list.py:256 also sets rfp=1 on **all** rows on submit. Data: 0 such rows today (`rfp=1 AND awaiting=1` → 0); latent. | Set rfp = 0 when awaiting_transfer = 1. Define "at hub" from the transfer flags, not rfp. |
| TR-7 | High | Filter/KPI | **The two tabs disagree on what is "scheduled".** `_schedule_map` adds every OPL that has a team and delivery_date ≥ today to the end of its team's queue (`from_opl`). Transfer Scheduling therefore lists them as scheduled ("Scheduled orders" KPI, Distribute steps). The Scheduler shows the same OPLs under "Not scheduled". In practice `unscheduled` here only ever holds no-team orders, or teamed orders delivered before today. The fallback sequence (max seq across **all** schedule days in 14 days, +1) also isn't the Scheduler's sequence. | transfer_scheduling.py:175-221 (fallback 206-220). scheduler.html:255-256 (`S.scheduled` from getScheduledOrders for the processing day). | One definition: scheduled = on the Packhouse Schedule for processing day = delivery − 1. If the fallback stays, flag it separately (`auto_queued`) and count it apart in the KPIs. |
| TR-8 | Med | Math | **The order type label (Straight / Mixed box / Mixed bunch) comes from stale `Sales Order Item.custom_opl`.** 57 SOI rows point at OPLs belonging to a *different* Sales Order, so the label is taken from foreign lines. | `_mixed_map` transfer_scheduling.py:144-162 (same in scheduler.py:150-170). Data: OPL-2026-00030 (SO-95) labelled **Mixed bunch** via SO-176's lines, but its own lines (via PLI.sales_order_item) are 0/0 = Straight. OPL-2026-00018 and OPL-2026-00020 labelled **Mixed box**, actually 0/0. `SELECT COUNT(*) … WHERE soi.parent <> opl.sales_order` → 57 rows / 51 OPLs. | Derive the type through `PLI.sales_order_item` (or `opl.mix_group` / `opl.bunch_group`), never `SOI.custom_opl`. |
| TR-9 | Med | Math | **Stems on trips and distributions are estimates, not counts.** Every planned row's `stems` = round(avg stems per bucket at that farm × buckets taken). The average mixes varieties and bucket sizes. Trip `total_stems`, Bucket Distribution `total_stems` and the auto summary all store these estimates. Only `loaded_stems` is real (from PLI). | _planner_common.html:279, 378. transfer_scheduling.html:556, 576. auto_transfer.py:384. | Plan specific buckets, or return per-bucket stems and sum the chosen ones. Show "planned ≈" vs "loaded" separately. |
| TR-10 | Med | Math | `_truck_status` counts **PLI rows**, not buckets (`st["total"] += 1` per row). A multi-variety or multi-box bucket counts N times in "Loaded x / y". RDHWGJ alone has 20 rows. It also doesn't exclude cancelled OPLs. | transfer_scheduling.py:387-460 (line 425). | COUNT DISTINCT UPPER(bucket) per truck and phase, joined to OPL with docstatus < 2. |
| TR-11 | Med | Math | **Variety breakdown:** a multi-variety bucket is put under the variety of its first row, with every row's stems added to it. | transfer_scheduling.py:262-263, 1755-1757. 46 (opl, bucket) pairs have >1 row. | Break down by (bucket, variety) for stems; count the bucket once per farm. |
| TR-12 | Med | KPI | **Hub shelf space** `on_road` = DISTINCT buckets with in_transit or loaded_in_trolley, not shelved or issued, over **all history**, with no date or `custom_ready_for_packing` filter. A stale in_transit flag (the code itself says it is "set once and never cleared") occupies a hub slot forever. `planned` is per (opl, farm), so split buckets are double-counted (TR-5). | transfer_scheduling.py:4534-4579. Data: per_shelf = 0 → capacity None, so the KPI shows "1637 shelves" only. | on_road = buckets on non-Received trips' `trip_buckets` not yet shelved. Planned = DISTINCT bucket claims. |
| TR-13 | Low | KPI | "Trips" KPI and windowTrips: a trip is counted under the page's delivery date only if one of its order rows has that delivery_date. Stale Draft trips are counted as "planned". | transfer_scheduling.html:137-139, 959-962 | Count stale trips separately. |
| TR-14 | Med | Integrity | **Read endpoints write and commit.** `getTransferScheduleData` (fetched as GET) runs `ensure_day_routes` (inserts day routes and commits, transfer_scheduling.py:1700, truck_routes.py:606-652). Per vehicle, `_vehicle_on_road` → `_settle_finished_trip` sets Received and commits (656-705). `getBucketLogisticsRoutes` and `addFarmTrip` also run `ensure_day_routes`. A page refresh can therefore change data, and does so per request. | as cited | Move day-route materialisation and trip settling to the scheduler, or to explicit POSTs. Keep the feed pure. |
| TR-15 | Med | Integrity | **auto_transfer partial commits.** `_plan` calls `_clear_own_plan`. Each `_save_route` then commits (truck_routes.py:197), and `ensure_day_routes` commits. If a later `_save_trip` fails or raises, the `rollback()` in `plan()` can't restore the deleted drafts, leaving a half plan. `runNow` also runs the whole feed synchronously in the request. | auto_transfer.py:128-150, 215-236 | Build the plan, then write it in one transaction (no inner commits). Enqueue `runNow`. |
| TR-16 | Med | Integrity | `_transfer_schedule_payload` has no check on SO `docstatus` or status (Closed / On Hold / cancelled SO with a live OPL would still plan). | 1704-1723 | Join with `so.docstatus = 1 AND so.status NOT IN ('Closed','On Hold')`. Unverified on data (0 such OPLs). |

### Performance profile
- **Queries per request** (from the code). T = trips loaded (today + 7 days unreceived), V = internal vehicles, R = today's routes, S = Packhouse Schedules in 14 days, D = Bucket Distributions in the window (≤50), Dt = dispatched trips per vehicle:
  - `_schedule_map`: 1 + **S (N+1 per schedule)** + 1 (lines 183-200)
  - `opl_rows` 1, `_mixed_map` 1, `_left_behind` 1-3 (calls `_transfer_buckets` a **second time** via `_open_counts`, line 319), `_transfer_buckets` 1 (line 1738)
  - per vehicle: `_vehicle_on_road` 1 + Dt × (`get_doc` + settle), `get_value dispatched_at` 1, `_route_end` 2, `_day_runs` 1 + per route (exists + legs = 2) + **per run 1** (`_run_trip`)
  - per trip: `get_doc` (~3-4 with child tables) + due-dates 1 + `_trip_run_info` 3 + `_trip_stops` 2 → **~10 queries per trip** (line 1881)
  - per route: `get_doc` ~2. Distances 2. `_truck_status` 1 (EXISTS subquery per row). `_hub_company_farms` 2. `duplicate_trips` 1 + per legacy trip (`_day_runs` + orders) + **`get_value(run)` per trip** (line 3517). `_distributions` 1 + **D** (child query per distribution). `hub_shelf_space` 4. `hub_ready_orders` 2.
  - Total ≈ **25 + S + 10T + V(6 + 3R_v + runs) + 2R + D**. On kaitet (no trips, 1 truck, 0 schedules in 14 days) this measured **21 queries, 27 ms warm** (56 queries / 0.72 s first call, warm-up). A realistic day (20 trips, 3 trucks × 2 routes, 5 teams × 14 schedule days, 30 distributions) is ≈ 25 + 70 + 200 + 40 + 30 ≈ **370 queries**.
- **Repeated recomputation:**
  - The page reloads the full feed on every write (`RTP.changed`), before Add to trip (`RTP.load`), before Distribute, after addFarmTrip, and on the **Truck Routes** tab. `auto_transfer.plan` recomputes it every 5-10 min and on every OPL change (`replan_soon`).
  - `_transfer_buckets` runs twice per request. `transfer_hub()` runs per route and leg (cached doc, cheap).
  - On the client, `RTP.openTotal(o)` rebuilds `coverMap()` (all trips × rows) on every call: per order several times in KPIs, render, sort and stages, plus `orderRow` calls `coverMap()` again. That is O(orders × trips × rows) per render.
- **Full scans / missing indexes** (SHOW INDEX):
  - Pick List Item: no index on `transit_truck` or the flags. `hub_shelf_space.on_road` does a full PLI scan (EXPLAIN: type ALL). `UPPER(pli.bucket)` in joins/DISTINCT defeats the `bucket` index.
  - Order Pick List: **no index on `sales_order`**. `opl_rows` scans every OPL (EXPLAIN: opl ALL).
  - Bucket Request Trip: only name/creation/modified, so **no `trip_date`, `status`, `vehicle` or `route` index**, and every claim, holder and duplicate query scans.
  - Bucket Request Trip Order: **no `order_pick_list` index** (`_trip_claims`, `_left_behind`).
  - Packhouse Schedule (`schedule_date`, `team`) and Packhouse Schedule Order (`order_pick_list`): none.
  - Bucket Logistics Route: no `(route_date, vehicle)` index.
  - Sales Order Item: no `custom_opl` index.

### v2 backend design notes
- **Bucket state:** one view, `transfer_bucket(opl, bucket_upper, farm, state, stems, variety_stems, trip)`, built in one aggregate query over PLI (GROUP BY parent, UPPER(bucket)).
  - state ∈ {waiting (awaiting & !in_transit & !shelved & !not_found), trolley (loaded_in_trolley & !in_transit), on_road (in_transit & !shelved), arrived (shelved)}.
  - Don't use `custom_ready_for_packing` as "arrived" (TR-6).
  - Physical counts (truck slots, hub space, "on a truck") = COUNT DISTINCT bucket_upper. Order demand = per (opl, bucket).
- **Claims:** per (opl, farm), Σ GREATEST(buckets − loaded, 0) over active, non-stale trips (`_trip_claims`), returned by the server. Open = waiting − claimed. The client should not recompute it (fixes TR-4).
- **Schedule:** one function, `schedule_of(opl)` = the Packhouse Schedule for processing day = delivery − 1 (`schedule_date = so.delivery_date − 1`), one JOIN. Any auto-queued fallback is flagged.
- **Fleet:** transfer truck = `custom_is_internal_logistics_truck=1`, everywhere (TR-3).
- **Trips:**
  - Load trip docs with 2 bulk queries (parents with status/date filters, then all `Bucket Request Trip Order` rows `IN (…)`).
  - Load route legs once with `IN (…)` for all today's routes and compute runs in memory.
  - Fetch due dates with one `IN` query for all trips. This replaces ~10T queries with ~4.
- **Schedules / distributions:** 1 JOIN each instead of N+1.
- **Pure GET:** move `ensure_day_routes` and `_settle_finished_trip` to a scheduler job and to the write endpoints.
- **Indexes:**
  - `tabPick List Item(parent, awaiting_transfer, in_transit, shelved)`, `(transit_truck)`, and a stored or generated `bucket_upper` with an index.
  - `tabOrder Pick List(sales_order)`.
  - `tabBucket Request Trip(trip_date, status)`, `(vehicle, status)`, `(route, run)`.
  - `tabBucket Request Trip Order(order_pick_list, farm)`.
  - `tabPackhouse Schedule(schedule_date, team)`.
  - `tabPackhouse Schedule Order(order_pick_list)`.
  - `tabBucket Logistics Route(route_date, vehicle)`.
  - `tabSales Order Item(custom_opl)`.
- **Caching:** the feed is per-date. Key it on max(modified) of PLI flags and trips, or invalidate on the trip and PLI write hooks. Truck Routes should only fetch vehicles, distances and templates.

---

## Page: Scheduler (/remote-transfer-v2?tab=scheduler): API `api.scheduler.getSchedulerFeed`, `getScheduledOrders`, `saveDaySchedule`

### What the page shows and how it's computed (brief)
- **Feed:** every OPL with docstatus < 2 whose SO delivery_date equals `date`, dropped once every PLI row is issued.
  - Per OPL: `total_stems` = `OPL.custom_total_stems`. `n_buckets` = distinct `bucket` (case-sensitive). `farms` = pli.farm, else the source_warehouse prefix. `waiting[farm]` = distinct buckets with `awaiting_transfer=1`. Trips come from Draft/Scheduled trips on delivery − 1.
- **Scheduled set:** `getScheduledOrders(date − 1)`.
- **KPIs:** "Schedulable" = feed.length. "Not scheduled" / "Scheduled" are split by S.scheduled. "Total stems" = Σ total_stems (with distinct-customer count).
- **"Distribute to teams":** an order with no team goes to the team with the fewest scheduled `n_buckets`.
- **Save:** rebuilds each team's `PSCH-<date−1>-<team>` from the global order and clears teams no longer present.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| TR-2 | Critical | Integrity | (see above) The feed fails on every date (`takt_time`). The whole tab shows an error. | scheduler.py:61 | `custom_takt_time` |
| TR-18 | High | KPI/Math | **"Total stems" = `OPL.custom_total_stems`**: a varchar of *allocated* stems, stale or NULL on some drafts. It is not the ordered stems and not even the current PLI sum. | scheduler.py:244-247. Data, 2026-09-16 delivery: the feed's KPI = 0+0+0+0+120 = **120**. Current PLI stems = **160**. Ordered stems (SOI via PLI.sales_order_item) = **1,080** (OPL-29…32: NULL vs 10 picked vs 240 ordered each). OPL-2026-00048: custom_total_stems 600 vs PLI 580. | Order stems = Σ SOI stems-per-box × boxes (primer). Allocated = Σ PLI stock_qty. Show both, labelled. |
| TR-19 | High | Math | **"N bkt at farm" counts buckets that are already on the road.** `waiting` = `awaiting_transfer=1`, but the transfer lifecycle keeps awaiting=1 while loaded and in transit, until shelved. `orderState` therefore shows "x bkt at farm" for buckets on a truck, and "Coming in" is unreachable for them. It also ignores not_found and rfp. | scheduler.py:134-135 vs transfer_scheduling.py:31-38 (header: "awaiting_transfer stays 1"). scheduler.html:390-394. | waiting = awaiting & !loaded & !in_transit & !shelved & !not_found, the same as `_transfer_buckets.open`. |
| TR-20 | Med | Math | **Farm attribution differs between tabs.** The Scheduler uses `pli.farm` first. Transfer Scheduling uses the source_warehouse prefix first (`FARM_EXPR`). | scheduler.py:127-130 vs transfer_scheduling.py:55. Data: 66 PLI rows in ~25 OPLs have pli.farm = Kapkolia but source farm Chepsito (44), Simotwo (12), Torongo (7), Kaptumbo (3). E.g. OPL-2026-00026 is listed by the Scheduler with farms [Kapkolia, Torongo]. The farm filter, farm badges and per-farm waiting differ. | One farm rule (FARM_EXPR) shared by both. |
| TR-8 | Med | Math | Mixed-type label from stale `SOI.custom_opl` (proven: OPL-2026-00030 shows "Mixed bunch", actually straight). | scheduler.py:150-170, 225-231 | via PLI.sales_order_item |
| TR-21 | Med | Math | **Team load balancing** ("Distribute to teams") weights by `n_buckets`. That includes local and hub buckets and split buckets (TR-5), and ignores stems and boxes. Only teams that already have an order in this date's feed are candidates, so an idle team never receives work. | scheduler.html:497-513 | Balance on planned stems or boxes (Packing Guide) per team over the processing day. Candidates = all active Packing Teams. |
| TR-22 | Med | Filter | **Processing-day rule is only partly applied.** The Scheduler reads and writes the schedule at delivery − 1, and its trip lookup uses `trip_date = delivery − 1`. Transfer Scheduling instead uses "latest schedule in 14 days" (TR-7), and always creates trips dated *today* regardless of the delivery date viewed. A trip planned while looking at delivery D+2 is invisible in the Scheduler's suggestions for D+2. | scheduler.py:192; scheduler.html:91; _planner_common.html (trip_date: RTP.today()); transfer_scheduling.py:175-200 | Key everything on processing_day = delivery − 1. |
| TR-23 | Low | Perf/Integrity | `on_schedule` is computed (an extra query) and never used. The docstring ("drops off once ANY bucket is issued") contradicts the code (drops when all rows are issued). `n_buckets` is keyed case-sensitively while every other module uses UPPER (0 case variants today). `getScheduledOrders` does a `get_doc` per schedule (N+1). | scheduler.py:176-186, 41-44 vs 214, 119; 20-29 | Delete the unused query; one JOIN for the scheduled map. |
| TR-24 | Low | Integrity | `saveDaySchedule` rebuilds a team's doc only from orders in this delivery date's feed. Orders on the same `PSCH-<day>-<team>` for another delivery date (ASAP, moved delivery) are silently dropped. It is last-writer-wins with no version check. `set_value(team)` bypasses OPL validation. | scheduler.py:280-340 (writes analysed only) | Rebuild per (processing_day, team) from all OPLs whose delivery − 1 equals the day. Use an optimistic `modified` check. |

### Performance profile
- `getSchedulerFeed`: 5 queries per call, plus 1 + trip-rows when there are trips (measured 5 queries, 6-9 ms with the takt patch). It is fine, apart from the dead `on_schedule` query.
- `getScheduledOrders`: 1 + N `get_doc`s (N = teams that day). The page calls it on every load in parallel with the feed.

### v2 backend design notes
- A single "day board" query per (processing_day): OPL ⋈ SO (delivery = day + 1) ⋈ the PLI aggregate (shared bucket-state view) ⋈ the schedule (LEFT JOIN, in the same response, replacing the separate `getScheduledOrders`).
- Stems: return `ordered_stems` (SOI), `allocated_stems` (Σ PLI) and `planned_stems` (Packing Guide). Make "Total stems" the ordered number.

---

## Page: Truck Routes (/remote-transfer-v2?tab=routes): API `api.transfer_control.getRouteTemplates` (+ `RTP.load` = getTransferScheduleData), writes `saveRouteTemplate`, `setRouteTemplateActive`, `deleteRouteTemplate`, `saveFarmDistance`, `deleteFarmDistance`, `saveTransferTruck`, `addFarmTrip`

### What the page shows and how it's computed (brief)
- KPIs: "Routes" = active / saved templates. "Trucks" = internal vehicles, available vs on the road (from the planner feed). "Roads" = Farm Distance rows with `is_road_leg`. "Farms reachable" = farms with a road path from the hub, computed client-side with Dijkstra over `distances`.
- The list shows each template's runs and total_km.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| TR-17 | Med | Perf | **The tab loads the entire Transfer Scheduling feed** (see the Transfer Scheduling performance profile: trips, claims, hub space, distributions, all the N+1s) just to get vehicles, distances and farm_list. Any failure in it blanks this tab (TR-1). | truck_routes.html:62-66 | A light `getRouteSetup`: vehicles (incl. on_road), Farm Distance, farm list, templates. 4 queries. |
| TR-25 | Low | Perf | `getRouteTemplates` / `getBucketLogisticsRoutes`: a `get_doc` per template or route (N+1). `getBucketLogisticsRoutes` also writes (`ensure_day_routes`, TR-14). | truck_routes.py:494-500, 207-247 | One query for parents plus one for legs `IN (…)`. |
| TR-26 | Low | Math | "Trucks" KPI and route km: `total_km` sums the legs as stored. The Farm Distance network here is still the seeded 10 km star placeholder (12 rows, all 10.0 km, created 2026-09-15). Every km and "bestRoute" figure is therefore placeholder. "Farms reachable" depends on `farm_list` = the hub company's farms, which would also fail with TR-1. | tabFarm Distance data; transfer_scheduling.py:27-29 comment | Data task: real road distances. |

### v2 backend design notes
- Routes are configuration data: they belong in their own small endpoint and are cacheable (they change only on saves).
- Generate day routes from templates in the scheduler at 00:05, not in GET handlers.
