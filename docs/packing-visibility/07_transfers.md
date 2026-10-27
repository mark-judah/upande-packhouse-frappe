# Packing Visibility: remote-farm TRANSFERS (how they work, how they delay packing, what to measure)

Read-only research. Local site `kaitet.local`. Code roots: `P` = `apps/upande_packhouse/upande_packhouse`, `Q` = `apps/upande_quality/upande_quality`, mobile repos `/home/jk/Projects/upande-quality` (farm / cold-store app, "Bucket Requests") and `/home/jk/Projects/upande-packhouse` (packhouse app, "Bucket Logistics", "Scheduler", "Issuing").
Marker legend: **[V]** verified in code and local data, **[C]** code only, **[?]** uncertain / needs production check.
`TS` = `P/api/remote_transfer/transfer_scheduling.py` (alias path `api.transfer_control`), `AT` = `P/api/auto_transfer.py`, `SCH` = `P/api/remote_transfer/scheduler.py`, `STEPS` = `P/api/remote_transfer/transfer_steps.py`, `QAPI` = `Q/mobile/api.py`, `PAPI` = `P/mobile/api.py`.

## 0. Vocabulary and the actors in one picture

* Hub = `Production Settings.transfer_hub_farm` = **Kapkolia** (sales shelf). Sources = Torongo, Chepsito, Simotwo, Kaptumbo (`Production Settings.shelf_locations`: only Kapkolia and Karen are `sales_shelf=1`; Karen packs its own orders, never transferred: `SCH._own_packing_farms :10`). `TS.transfer_hub :87`. [V]
* Two different state machines:
  1. **Trip** (`Bucket Request Trip`, autoname `BRT-{trip_date}-{#####}`) `status` Select = Draft, Requested, Scheduled, Dispatched, Received. Meaning is NOT the one in the name: Scheduled = truck released from the hub and collecting at the farm(s); Dispatched = truck left its last farm loaded and is heading to the hub; Received = trip ended.
  2. **Bucket** (Pick List Item row of the Order Pick List, one row per box, so a bucket spans several rows): `awaiting_transfer` -> `loaded_in_trolley` (+`trolley_id`) -> `loaded_on_truck` / `transit_truck` -> `in_transit` -> `shelved` (then `custom_ready_for_packing`, `issued`). Header comment `TS :40-48`. Shelving clears awaiting/trolley/in_transit and sets `shelved=1` (`Q update_transit_status QAPI:1647`), so a "stage reached" test must use the cumulative `REACHED_*` expressions in `P/api/remote_transfer/bucket_logistics.py :30-50`.
* `Bucket Transfer Event` (BTE, naming hash; fields bucket, stage, outcome Done/Refused, event_time, user, farm, shelf, order_pick_list, trip, vehicle, stock_entry, details) is the append-only journal written by `TS.log_transfer_event :7655`; every Done event also stamps the PLI via `STEPS.on_event`.

Who does what (all by screen, not by role: the mobile apps gate nothing by role for these screens except the station/farm):

| Step | Who | Screen / endpoint |
|---|---|---|
| Allocate to remote bucket (sets `awaiting_transfer=1`, OPL draft) | Assistant Packhouse Manager / allocator | Desk "sales_allocation" page |
| Schedule the order onto a team (Packhouse Schedule) | Assistant Packhouse Manager (or auto, `SCH.auto_schedule :114`) | Desk Remote Transfers > Scheduler; mobile Scheduler (`getSchedulerData`, `setSchedulerOrder`) |
| Plan trips (Distribute) | Planner (Assistant Packhouse Manager) or auto-transfer cron | Desk Remote Transfers > Transfer Scheduling; `AT.distributeTrips :300`, `AT.run :66` |
| Request truck (Draft -> Requested) | Planner on Desk (`requestBucketTrip :6481`) or automatic for full trips | `TS.request_trip :6350` |
| Confirm / reject the request (Requested -> Scheduled / back to Draft with reason), change truck, dispatch, receive | Bucket logistics coordinator at the hub | packhouse app "Bucket Logistics": tabs Routes / Trip Requests / Trips (`BucketLogisticsScreen.tsx :22-25`); calls `releaseBucketTrip`, `rejectBucketTrip`, `changeTripVehicle`, `dispatchBucketTrip`, `receiveBucketTrip` (`karen-bucket-logistics-api.ts`) |
| Find bucket in cold room, scan into trolley | Remote farm cold-store attendant (station = their farm) | farm app "Bucket Requests" > Requests / Trolley tabs; `fetchAllocatedBuckets QAPI:2557`, `saveTrolleyData QAPI:9255` (BTE "On trolley"), offline `setOfflineTrolleyFlags QAPI:9350` |
| Load trolley on the planned truck | same attendant | `loadTrolleyInTruck QAPI:6837` -> `TS.record_truck_load :2964` (BTE "Loaded on truck" via offline path `QAPI:9530`) |
| Bucket not in the cold room | same attendant (after replacement search) | `markRequestedBucketNotFound` -> `TS.mark_bucket_not_found :5830` |
| "Truck leaving" (close this farm's stop, truck moves on or is dispatched to hub) | same attendant | `closeTripStop :5955`, BTE "Left the farm" per bucket :6045 |
| Truck at hub: confirm arrival / complete trip | farm app In Transit tab, hub side people | `tripArrival :6702` (actions status/arrive/complete) |
| Shelve at hub (the real arrival) | Hub cold-store / shelving staff (Stock User, Coldstore Attendant) | farm app "Shelving" -> `createShelvingEntry`/`shelveBucket`; sets `shelved=1`, BTE "Shelved at sales farm" (`QAPI :2321`) |
| Issue to the order, pack | Issuer / packers (team) | packhouse app Issuing / Packing |

Bucket Requests is hidden for a station that IS the hub (`/home/jk/Projects/upande-quality/app/_layout.tsx :56`); it only has `Freight Dispatch` role-gated. Hub-side people therefore do shelving and the In Transit tab arrival from Shelving / Bucket Transfers. [C]

---

## 1. The complete transfer lifecycle

### 1.1 Trip lifecycle (Draft -> Requested -> Scheduled -> Dispatched -> Received)

| State | How it is reached (file:line) | Fields written | By whom |
|---|---|---|---|
| (create) Draft | `AT._save_plan :1195` -> `TS._save_trip :4722`/`_write_run_trip :5121` (planner, auto `auto_planned=1`), or `TS._new_load_trip :3714` ("unscheduled" trip made by a load nobody planned: `unscheduled=1`) | `vehicle, route, run, trip_date, farm, orders[] (order_pick_list, farm, buckets, stems, full_farm_buckets, is_partial), total_buckets, total_stems, capacity_buckets`; `creation` = "scheduled_at" | planner / auto |
| Draft -> Requested | `request_trip :6350` (refuses if trip_date < today) | `requested_by, requested_at`, clears rejection, sets `auto_planned=0` | planner on Desk; with `Production Settings.release_trips_without_confirmation` the request releases at once (:6366) |
| Requested -> Draft (rejected) | `reject_trip :6381` | `rejected_by, rejected_at, reject_reason` | hub coordinator (RejectTripSheet, reason mandatory) |
| Draft/Requested -> Scheduled (released: truck leaves hub for farms) | `release_trip :6398`; refuses if truck still out (:6413), no truck, earlier day | `released_by, released_at`, `auto_planned=0`; then `rank_after_dispatch :6473` re-ranks team schedules | hub coordinator ("Confirm release"); or `dispatch_full_trips :6450` for FULL Draft trips when `auto_dispatch_full_trips` AND `auto_remote_transfer_scheduling` (`auto_dispatch_enabled :6440`) |
| (loading at farm) | `record_truck_load :2964` adds `Bucket Request Trip Bucket` rows (bucket, order_pick_list, farm, `loaded_at`, `loaded_by`, `shelved`, `shelved_at`, `off_truck`) and `loaded_buckets`; `_advance_stops :2855` | `departed_stops` (CSV), `last_departed_at`, `heading_to` | farm app load |
| Scheduled -> Dispatched | (a) last stop closed: `closeTripStop :5955` -> `_dispatch_last_stop :2931`; (b) every stop loaded and truck full: `_advance_stops :2855`; (c) manual `dispatchBucketTrip :6251` (needs reason below minimum load, `below_minimum :3841`); refuses while the truck is out on another trip (`_vehicle_on_road :978`, then waits: `_dispatch_waiting_trips :1648`) | `dispatched_at`, `heading_to=hub`; `_put_loads_in_transit :6327` sets `in_transit=1, transit_truck` on all rows of every loaded bucket (also for shared buckets) | farm attendant ("Truck leaving"/"Send to packhouse") or hub coordinator ("Dispatch") |
| Dispatched: arrived | `_mark_arrived :6612` (BTE "Arrived at packhouse" for each still-on-truck bucket); triggered by (i) tripArrival action `arrive` :6702, (ii) FIRST bucket of the trip shelved at the hub (`auto_arrive_for_bucket :6648`, `ensure_arrived :6632`), (iii) 5-minute sweep `end_shelved_trips :1805` | `arrived_at`, `arrived_by` | hub staff / shelving scan |
| Dispatched -> Received | `receiveBucketTrip :6530` (manual "truck is back": also sets `arrived_at` if empty), `tripArrival complete` (needs every carried bucket shelved), `_end_trip_if_shelved :1111` (all carried buckets shelved/off truck), `_end_trip_for_newer_run :1780`, midnight close | `received_at`; `carry_over_to_next_run :2341` moves left-behind buckets to the truck's next run | auto or hub |

IMPORTANT semantic: **arrival at the hub is detected by shelving**. The "arrived" time of a trip is when someone first shelves one of its buckets (or presses arrive). There is no GPS arrival. A truck that physically arrived but is not unloaded/shelved looks "in transit" until then. [C]

### 1.2 Bucket lifecycle (what each flag/stamp means, where written)

| Stage | Flag on PLI | Stamp (Pick List Item) | Written by | Event (BTE stage) |
|---|---|---|---|---|
| Allocated at remote farm | `awaiting_transfer=1`, optional `transfer_priority='ASAP'` | `creation` of the row (no dedicated stamp) | allocation; ASAP set by quality replacement `P/api/packing_quality.py :663` | - |
| In trolley | `loaded_in_trolley=1`, `trolley_id`, `awaiting_transfer=0` | `loaded_in_trolley_at/_by` | `saveTrolleyData QAPI:9255`, `setOfflineTrolleyFlags QAPI:9350` | "On trolley" (saveTrolleyData :9296) / "Loaded in trolley" (offline). **Gap**: `STEPS.STAGE_STEP :34` only maps "Loaded in trolley", not "On trolley", so a trolley scan via saveTrolleyData has no on_event stamp; `loaded_in_trolley_at` comes later from `reconcile` at the row's `modified` [C, ?] |
| On truck | `loaded_on_truck=1`, `transit_truck=<Vehicle>` (only internal Vehicles count: `TRANSFER_TRUCK_OK`, `custom_dispatch_truck=0`) | `loaded_on_truck_at/_by`; also `Bucket Request Trip Bucket.loaded_at` | `loadTrolleyInTruck :6837` -> `record_truck_load` | "Loaded on truck" |
| Left the farm | `in_transit=1` | `left_farm_at/_by` | `closeTripStop` (per farm), `_dispatch_last_stop`, `dispatchBucketTrip` | "Left the farm" (:6045). A plain Dispatch press logs no event: stamp via reconcile at `modified` |
| Arrived at hub | (no own flag; trip `arrived_at`) | `arrived_at/_by` | `_mark_arrived`, or equal to shelving | "Arrived at packhouse" |
| Shelved at hub | `shelved=1`, awaiting/trolley/in_transit cleared, `shelf` set, `source_warehouse` moved to hub store (`update_transit_status`) | `shelved_at/_by`; `Bucket Request Trip Bucket.shelved/shelved_at` | farm/hub app shelving | "Shelved at sales farm" (QAPI:2321); "Shelved at remote farm" is NOT an arrival |
| Issued | `issued=1` | `issued_at/_by` (written by 5-minute reconcile at row `modified`) | issuer | - |
Other BTE stages: "Back on farm shelf" (return_to_farm :7496), "Load refused", "Shelving refused" (remote_shelving_block :7536 / hub_shelving_block :7593), "Load recorded on arrival" / "Arrived without a trip" (`record_planned_load_on_arrival :1320`), "Carried to next trip" (:1746), "Not shelved when trip ended" (:1911), "Anomaly: left overnight" (`LEFT_OVERNIGHT :2043`), "Removed from wrong shelf". Stamps are write-once (`STEPS.stamp :61`); `STEPS.reconcile :102` + cron `*/5` (`hooks.py :304-311`) fills missing ones from the matching Done event, else from the row's `modified/modified_by`.

### 1.3 What triggers a trip, and the rules
* Trigger (automatic): `Order Pick List` `after_insert` / `on_update` hooks (`hooks.py :261-270`) -> `AT.replan_soon :443` (queues `AT.replan`, deduplicated) when a pick list is new, changes team, or its awaiting buckets change (`AT.order_pick_list_changed :466`). Plus cron `*/5`: `AT.run :66` which only re-plans when `Production Settings.auto_remote_transfer_scheduling=1` and `auto_transfer_frequency` minutes (default 10, `DEFAULT_FREQUENCY_MINUTES`) have passed. Manual: Desk "Distribute" = `distributeTrips :300`, "Re-plan now" = `runNow :423`. [V locally: setting = 1, `auto_dispatch_full_trips`=1, `release_trips_without_confirmation`=0]
* Trigger (physical): a bucket loaded on a truck with no open planned trip creates an "unscheduled" trip on the truck's next run (`_new_load_trip :3714`, `unscheduled=1`).
* Rules:
  * **One farm per trip**: `_save_trip` refuses "Trip X goes to one farm" (`TS :4826`); each run of a truck's route (hub -> farm -> hub) is one full-truck trip collecting at one farm (`AT` header :14-19). A farm gets a second trip only when its first (any truck) is full or has left it (`farm_holders :4477`, `_clashes :1061`). Several stops are only possible on hand-made multi-farm routes (`_trip_stops :2554`).
  * **Capacity**: `capacity_buckets = Vehicle.custom_trolley_capacity * Vehicle.custom_buckets_per_trolley` (`_vehicle_capacity :3805`, `_trip_capacity :3797`); over capacity refuses the whole save (reason `over_capacity`). No-partial-loads: an order portion that does not fit whole waits for the next run (`AT._distribute_steps :810`). Min load: truck sends only full unless `allow_partial_truck_load` with `min_truck_load_pct`, else a reason is required (`min_load_needed :3820`, `below_minimum_refusal :3853`).
  * **Truck at a time**: a truck on the road (Dispatched, not arrived) cannot be re-planned/dispatched (`_on_road_message :1058`).
  * **Midnight close**: cron `0 0 * * *` -> `close_previous_day_trips :1865` (hooks.py :298): every non-Received trip dated before today is ended; Dispatched = Received with unshelved buckets logged "Not shelved when trip ended"; Draft/Requested/Scheduled that never left are closed and their loaded buckets become anomalies (`keep_as_anomaly :2048`: trolley and truck flags cleared, NOT put back on a shelf, NOT waiting for a truck; BTE "Anomaly: left overnight" with trip/truck/SO/delivery; comment on OPL and trip). Then `clear_left_trolleys :1929` does the same for trolley-only buckets (not in transit/shelved/not found).
  * **Stale trip**: `_trip_dict :764` `stale=1` when trip_date < today, status in Draft/Requested/Scheduled and nothing loaded; shown on the coordinator screen (`RawTrip.stale`); it claims nothing and should be deleted/re-planned.
  * **Keep as anomaly** = the above; **no flag on the PLI** identifies an anomaly bucket; find via BTE stage `Anomaly: left overnight` (and the PLI then reads awaiting_transfer=0, loaded_in_trolley=0, not shelved, not issued).
  * **Left behind**: planned on a trip that left the farm (stop closed/dispatched) without loading them -> `_left_behind :430`, planned FIRST on the next trip to that farm (`AT._place_left_behind :757`); short reason stored on `Bucket Request Trip Order.short_reason`.
  * **Not found**: `mark_bucket_not_found :5830` sets `not_found=1`, clears awaiting/trolley, creates a `Bucket Watch` (53 locally), and `_plan_one_fewer :5923` reduces the trip's planned buckets so the stop can complete.
  * **Hold at last stop**: `_hold_for_waiting :2794` keeps a part-full truck loading at its last farm if scheduled buckets are waiting there.

### 1.4 Scheduler / auto-transfer logic
1. **Demand** = scheduled OPLs (on a Packhouse Schedule row for delivery D, processing day D-1) with buckets still open at a remote farm: `TS._schedule_orders :4016` (window = delivery tomorrow by default; also ASAP replacements of any later delivery date). Feed: `getTransferScheduleData` (`TS :3991`).
2. `AT._auto_schedule :285` first adds ready orders to team schedules for tomorrow and the day after (`SCH.auto_schedule :114`; skips orders with no team and `draft_plain`).
3. `AT._plan :511`: builds road graph from `Farm Distance` (12 rows locally, `distance_km`, `is_road_leg`), `_fleet :603` = only **internal logistics trucks with capacity AND a `Bucket Logistics Route` for today** (no route = not working), then `_distribute :798`: ASAP replacements first (`_place_left_behind(key="asap")`), then left-behind buckets, then `_distribute_steps :810`.
4. **Sequence step logic** (this is the "transfer priority"): step k = every team's schedule #k. The first step not fully at the hub is planned; step k+1 waits until step k has arrived ("the hub shelves one step while the line issues the one before"). Within a step portions are ordered smallest first, whole or wait; a team keeps its queue per farm (blocked set). An order no truck can reach is reported (`unreachable`) but does not hold others back.
5. After distribution: `_top_up :951` (trucks topped up with the day after's orders so a trip leaves full), `_consolidate :974`, `_save_plan :1195` (re-plans only its own Draft trips; trips a person saved keep their orders and are topped up), `log_distribution :4293` writes one `Bucket Distribution` per run (source Automatic/Manual; 0 rows locally), `dispatch_full_trips`, then `rank_schedules` for the processing day.
6. `AT._plan_waiting :83` / `_add_runs_for_waiting :315`: buckets no open trip has room for get a NEW run added to a truck's route (also while it is out), up to 3 rounds; `_place_scheduled :108` places every scheduled order's remaining buckets, only for orders delivering tomorrow or the day after (`> today`).
7. **Priority**: (a) `transfer_priority='ASAP'` (quality replacement from packing, `P/api/packing_quality.py :663`) planned for any delivery date; (b) left-behind; (c) schedule step number (team's queue position), earliest delivery first in `_scheduled_portions :152`; (d) smaller portion first. There is no customer priority field.
8. Dispatch stays manual unless `auto_dispatch_full_trips` (only FULL Draft trips auto-request+release).
9. Cron cadence: `*/5`: `AT.run`, `end_shelved_trips`, `rank_schedules`, `transfer_steps.run`; `0 0`: `close_previous_day_trips` (`hooks.py :296-311`).

### 1.5 Failure surfaces (how things go wrong and where it shows)
| Failure | Mechanism | Where visible |
|---|---|---|
| Truck late / trip never left | Trip stays Draft/Requested/Scheduled past its route window (`Bucket Logistics Route.from_datetime/to_datetime`); at midnight becomes `stale` then closed | coordinator screen `stale` badge; `Bucket Request Trip.status`; dashboard query 4.3 |
| Truck left but buckets not shelved | status Dispatched, `arrived_at` NULL or arrived but buckets unshelved | `tripArrival status`; Bucket Transfers screen `getInTransitBuckets QAPI:4501`; query 4.5 |
| Bucket missing | `not_found=1` + `Bucket Watch` (Watching/Reappeared) | OPL comment; `rt_watchlist`; PLI `not_found_at/_by` |
| Bucket left in trolley / loaded on never-leaving truck | anomaly | BTE `Anomaly: left overnight` |
| Short load | `Bucket Request Trip Order.short_reason`, comment "Held at ... min load", `below_minimum` flag in dispatch response | trip comments |
| Planner cannot fit | `AT` summary: `waiting[{why: "trucks full..." / "truck room at F: n needed, m fit"}]`, `unreachable[]`, `failed[]` (cache key `upande_packhouse:auto_transfer:last_run`, returned by `AT.status :56`) | Transfer Scheduling page "auto" status |
| Planner run failed | `frappe.log_error("Automatic remote transfer scheduling failed")` | Error Log |

---

## 2. How a transfer delays packing

### 2.1 Hard gates
1. **OPL stays a draft**: `opl_submit_blockers` (`P/upande_packhouse/page/sales_allocation/sales_allocation.py :2245`): any row with `awaiting_transfer` OR `in_transit` OR (`loaded_in_trolley` and not `shelved`) blocks submit ("N bucket(s) are still in transit / not yet shelved at the sales farm"); also stem-coverage vs the GLOBAL confirmed stems and whole mix/bunch group allocated. `short_accepted=1` skips the coverage check but NOT the transit check (returns before coverage only). Called from every submit path including `Order Pick List.before_submit` and, at the moment of the last shelving scan, `check_and_submit_opl QAPI:1766` -> `_try_submit_opl_if_complete`. So **the last remote bucket's shelving scan at the hub submits the order**.
2. **Issuers do not see it**: `getReadySaleOrderItems PAPI ~:3510-3524` drops a DRAFT OPL from the issue list while any of its rows is awaiting/in trolley/in transit; the hub-held part of a mixed order is not issuable either. Packing lists show only submitted OPLs. [C]
3. **Team queue blocks the next order**: `_schedule_blockers PAPI:3266`: an order cannot be issued (`issueBucketToSaleOrderItem` returns 409 `schedule_blocked`, `PAPI :4898`) or packed (`_throw_if_pack_blocked :990`, "pack #n first") until every earlier order on its team's Packhouse Schedule is fully issued / fully packed. **Drafts waiting for a transfer count as earlier orders** ("every order is scheduled"). A not-found-only line never blocks. Teams never wait on each other; only orders at the same stock location (`_stock_locations :2245`: Karen vs Ravine/hub) hold each other. Bypass flags `allow_issue_out_of_sequence` / `allow_pack_out_of_sequence`: both 1 locally (so the block is OFF locally) [V]; production value unknown [?].
4. **Cascade to the team's schedule order**: `SCH.rank_schedules :160` (cron every 5 min and after every release/dispatch) re-sorts each team's Packhouse Schedule Order `sequence` by `_tiers :24`: 0 fully issued; (1,0) submitted; (1,1) draft with all buckets already at hub; (2, ...) still coming: trips that already left (Dispatched, by `dispatched_at`), then Scheduled (by `released_at`), then Draft/Requested by run, then no-trip by farm. An order waiting at several farms goes with the farm that arrives LAST. So "packing follows the trucks": a team's late-arriving order sinks to the bottom, ready orders float up. `Order Pick List.team` is stamped at allocation from the Sales Order Item (`sales_allocation.py :1876`); transfers do not change the team, they change the team's SEQUENCE. A team whose #1 is a not-arrived order, with allow_*_out_of_sequence = 0, is stuck until the truck arrives (all later orders blocked).
5. The planner itself is gated by the same ladder (`_distribute_steps`): step k+1 is not planned until step k has arrived, so the hub deliberately receives one step at a time. Slow arrival of step k therefore delays the whole next step's trucks.

### 2.2 What packers/issuers see while waiting
* Mobile Scheduler (`SchedulerScreen.tsx :167-195`, `getSchedulerMeta where{}` `PAPI ~:4056`): each order pill "At Kapkolia" (hub), "At farm" (remote), issued. The Scheduler feed `SCH.getSchedulerFeed :235` shows draft_transfer orders (zero issued) with stems, buckets, farm count and transfer state; once ANY bucket is issued the order drops off.
* Issuing list: nothing (draft hidden). Packing list: nothing (draft hidden).
* Remote farm attendants see a countdown-style run board: `getFarmPlannedTrips QAPI:3958` ("who is holding up the run": per stop loading status) and the Bucket Requests tabs (Requests, Trolley, In Transit with arrival ticks, Shelved).
* Coordinator sees Trip Requests / Trips with live per-stage counts (`getTransferScheduleData`, `bucket_status` awaiting/loaded/in_transit/shelved).
* **"Pack while waiting" options** (all about NOT-FOUND/replacement or partially issued orders, not about trucks in transit):
  * `_submit_for_packing PAPI :2100`: after an issue scan a DRAFT order is submitted once nothing is moving (`st["moving"]` false) and issued % >= `pack_waiting_replacement_pct` (default 50, `_pack_waiting_min_pct :3427`) or all non-not-found buckets are issued. So the hub-held part of a draft with no moving rows can be issued and packed; with any bucket still moving it cannot.
  * `server_scripts/pack_while_waiting_replacement.py`: sets `Order Pick List.pack_issued_now=1`, packs what was issued, replacement packed in later; `_issued_cap_error :3433` limits packing to issued stems.
  * `completeOplShort` sets `short_accepted` and submits.
  * `Under Pack Reason` master includes "Awaiting transfer from another farm": a reasoned box no longer holds the Farm Pack List draft (doc 01).
  * Transfers themselves have NO "pack while in transit" option: a bucket on the road blocks submit by design.

### 2.3 How late trucks compress packing into the evening (quantified, local data)
Local data limits (see section 3): real-time stamps are absent, but PLI `modified` of issued rows is the issue-time of the row (issuing is the last write on the row for most rows). Distribution of row `modified` for `issued=1` rows (JTEST excluded):
* Hub (Kapkolia) rows: 2,574; 73.6% modified before 12:00 (9-11h: 1,816 rows), 22.1% from 13:00 on.
* Remote-farm rows (Torongo, Simotwo, Chepsito, Kaptumbo): 239; only 31.8% before 12:00 (10h 62, 11h 14); 13h 72, 14h 35, 16h 10, **17h 36**; 65.7% from 13:00 on. 36 of the 51 rows issued at 17:00+ are remote.
* Remote `shelved=1` rows by `modified` hour: 10h 91, 11h 41, 13h 125, 14h 52, 16h 20, 17h 65, 18h 15: arrivals come in two waves (late morning and 13-14h), plus an evening wave at 17h (65 rows) and 18h.
* Issue happens the day before delivery (processing day D-1): 2,530 of 2,556 hub and 81 of 82 Torongo issued-bucket rows issue on D-1 (`date(modified) = delivery_date - 1`).
Interpretation: remote orders reach issuers about 3-4 hours after hub orders and the last remote orders land at 17:00+, which is exactly the compression management wants shown. [V as a pattern; the absolute timestamps are approximations, see 3.4]

---

## 3. Remote vs hub, local volumes, lead times

### 3.1 Which farms/shelves
* Hub: Kapkolia (`transfer_hub_farm`, sales shelf). Own-packing: Karen (sales shelf). Remote sources (Production Settings shelf_locations, `sales_shelf=0`): Chepsito, Simotwo, Torongo, Kaptumbo. A remote bucket's farm is `FARM_EXPR (TS :59)`: pli.farm before it is shelved, else the warehouse farm; after shelving its `source_warehouse` moves to the hub store and `origin_warehouse` keeps the source farm (434 rows have it locally).
* Fleet (local): Vehicle `KDT 177A` custom_trolley_capacity 4 x custom_buckets_per_trolley 20 = capacity 80, `custom_is_internal_logistics_truck=1`, but ALSO `custom_dispatch_truck=1`, which makes `TRANSFER_TRUCK_OK` treat it as NOT a transfer truck (data issue locally [V]); `KCF 700Z`, `KDP 283L` capacity 0. `Vehicle.employee` (driver) NULL for all. `Bucket Logistics Route` 0 rows, `Farm Distance` 12 rows, `Bucket Distribution` 0 rows, `Bucket Logistics Route Template` 0.

### 3.2 Local volumes per farm (PLI rows of OPLs, docstatus < 2, JTEST excluded; all-time)
| Farm | Rows | Buckets | Stems | shelved rows | still awaiting | issued rows | rows with origin_warehouse |
|---|---|---|---|---|---|---|---|
| Kapkolia (hub) | 5,050 | 2,659 | 416,975 | 0 | 0 | 2,599 | 55 |
| Karen | 436 | 231 | 22,150 | 0 | 0 | 309 | 1 |
| Torongo | 280 | 217 | 29,110 | 215 | 22 (2 draft OPLs, 11 buckets, 1,790 stems untouched since 10-08 and 09-11) | 82 | 205 |
| Simotwo | 163 | 137 | 23,100 | 150 | 0 | 90 | 107 |
| Chepsito | 118 | 105 | 19,156 | 116 | 0 | 67 | 64 |
| Kaptumbo | 6 | 6 | 940 | 5 | 0 | 0 | 2 |
Remote OPL count: 99 OPLs (95 submitted, 4 draft); teams of remote OPLs: Eldama A 43, Jamafa 22, Team A 17, Team B 16, Eldama B 3, Eldama 1, none 2. Remote share of buckets about 14% (465 of about 3,350). Remote rows: 81 not shelved (22 awaiting + stale ones), 247 shelved not issued, 239 issued. `transfer_priority='ASAP'` 1 row (local).
Per shelved date (remote buckets shelved, by farm): peaks 10-06 (Simotwo 42, Torongo 43), 10-07 (Torongo 59, Chepsito 24), 10-10 (Torongo 42, Simotwo 23).

### 3.3 Trip / event data locally
`Bucket Request Trip`: 3 rows, all 2026-10-23 JTEST for vehicle `KCF 700Z` (Dispatched 1, Received 2, all 0 buckets planned, `unscheduled=1`, no requested/released stamps, dispatched 3, arrived 2, received 2). `Bucket Request Trip Bucket`: 3. `Bucket Transfer Event`: 9 rows, 2 distinct buckets, all 2026-10-23 00:53-00:58 (On trolley 3, Arrived at packhouse 2, Shelved at sales farm 2, Shelved at remote farm 1, Removed from wrong shelf 1). In that test: On trolley 00:53:18 -> Arrived 00:54:52 -> Shelved 00:54:52 (about 1.5 min, synthetic). **There is no real trip/event history locally**: the April-October pick rows were imported or created before trips existed. [V]

### 3.4 Lead times (honest answer)
* Request-to-arrive, arrive-to-shelve, shelve-to-issue cannot be computed from local data. PLI stamps: `loaded_in_trolley_at`, `loaded_on_truck_at`, `left_farm_at` are NULL on every row; `arrived_at` and `shelved_at` populated on 446 rows (all remote, 2026-10-02..10-23), `issued_at` on 2,731 (10-02..10-11), `not_found_at` 58. For all 210 rows that have `shelved_at` and `issued_at` we find `arrived_at = shelved_at = issued_at = modified` exactly: these stamps were back-filled by `STEPS.reconcile` at the row's last change, NOT recorded at the real event. Treat them as "row last changed", not as event times.
* Usable proxy: allocation (PLI `creation`) to the row's stamp: Torongo avg 16 h (min 1, max 99), Chepsito avg 15 h (1-55), Simotwo avg 5 h (1-32), Kaptumbo avg 54 h (19-78). Order creation is usually the day before delivery at 08:00-13:00 and the shelved/issued stamps cluster 10:00-17:00 that same day, i.e. about 1-9 h typical for Simotwo/Chepsito/Torongo same-day cases, with multi-day tails for stuck drafts.
* On production, real lead time must come from `Bucket Transfer Event` (event_time of stages On trolley / Loaded on truck / Left the farm / Arrived at packhouse / Shelved at sales farm) joined to `Bucket Request Trip` (`requested_at`, `released_at`, `dispatched_at`, `arrived_at`, `received_at`) and `issued_at`. [?] whether production has these populated: unknown.

---

## 4. SQL and thresholds for a dashboard (SELECT only; JTEST filter local only)

Common: `pli` = `Pick List Item` joined to `Order Pick List o` (`o.docstatus < 2`), `so` = Sales Order for `delivery_date`. Excluding packed: `NOT (IFNULL(pli.custom_ready_for_packing,0)=1 OR IFNULL(pli.issued,0)=1)` (`PACKED_SQL`). Exclude `not_found=1`. Use `FARM_EXPR` (`TS :59`) for the farm. Stems = `stock_qty`; one bucket spans several rows so count `DISTINCT UPPER(bucket)`.

### 4.1 Buckets/stems awaiting transfer by farm and age
```sql
SELECT pli.farm,
       COUNT(DISTINCT UPPER(pli.bucket)) buckets, SUM(pli.stock_qty) stems,
       MIN(pli.creation) oldest_alloc,
       MAX(TIMESTAMPDIFF(HOUR, pli.creation, NOW())) max_age_h,
       MIN(so.delivery_date) next_delivery
FROM `tabPick List Item` pli
JOIN `tabOrder Pick List` o ON o.name=pli.parent AND o.docstatus<2
JOIN `tabSales Order` so ON so.name=o.sales_order
WHERE pli.parenttype='Order Pick List' AND pli.awaiting_transfer=1 AND IFNULL(pli.loaded_in_trolley,0)=0
  AND IFNULL(pli.in_transit,0)=0 AND IFNULL(pli.shelved,0)=0 AND IFNULL(pli.not_found,0)=0
  AND IFNULL(pli.issued,0)=0 AND IFNULL(pli.custom_ready_for_packing,0)=0
GROUP BY pli.farm;
```
Thresholds (proposed, none exist in code): amber when age > 6 h, red when age > 12 h or `delivery_date <= CURDATE()+1` and no trip; ASAP rows (`transfer_priority='ASAP'`) red immediately. Also "no trip yet": `Bucket Request Trip Order.unscheduled=1` or order absent from any Draft/Requested/Scheduled trip (`bucket_logistics.no_trip_section :56`, `NOT_SCHEDULED`). Existing endpoints: `getBucketLogistics`, `rt_logistics.get_logistics_view`.

### 4.2 Trips not departed at time T
```sql
SELECT t.name, t.vehicle, t.status, t.trip_date, t.run, t.farm, t.total_buckets, t.capacity_buckets, t.loaded_buckets,
       t.requested_at, t.released_at, r.from_datetime AS window_start, r.to_datetime AS window_end,
       TIMESTAMPDIFF(MINUTE, COALESCE(t.released_at, t.requested_at, t.creation), NOW()) age_min
FROM `tabBucket Request Trip` t
LEFT JOIN `tabBucket Logistics Route` r ON r.name=t.route
WHERE t.trip_date <= CURDATE() AND t.status IN ('Draft','Requested','Scheduled')
  AND (t.departed_stops IS NULL OR t.departed_stops='');
```
Flags: `stale` = `trip_date < CURDATE()` and nothing loaded; "late to leave" = `NOW() > r.to_datetime` or (status Scheduled and `released_at` older than the route window); "request not answered" = `status='Requested'` and `requested_at` older than 15 min (proposed). `Bucket Request Trip.farm` is a Data field (one farm per trip), `departed_stops` CSV tells which stop is done.

### 4.3 Trucks en route with ETA
```sql
SELECT t.name, t.vehicle, t.farm, t.heading_to, t.dispatched_at, t.last_departed_at,
       TIMESTAMPDIFF(MINUTE, COALESCE(t.last_departed_at,t.dispatched_at), NOW()) minutes_out,
       (SELECT SUM(1) FROM `tabBucket Request Trip Bucket` b WHERE b.parent=t.name AND IFNULL(b.shelved,0)=0 AND IFNULL(b.off_truck,0)=0) on_truck,
       (SELECT SUM(1) FROM `tabBucket Request Trip Bucket` b WHERE b.parent=t.name AND IFNULL(b.shelved,0)=1) shelved
FROM `tabBucket Request Trip` t
WHERE t.status='Dispatched' AND t.arrived_at IS NULL AND t.trip_date >= CURDATE() - INTERVAL 7 DAY;
```
ETA: there is NO ETA field. Compute it as `last_departed_at + km/speed` using `Bucket Logistics Route Leg.distance_km` (or `Farm Distance.distance_km`) with an assumed speed, or median historic `left_at -> arrived_at` per farm (`rt_logistics` already derives `transit_minutes`, `_trip_state :81` = not_left / in_transit / arrived). A GPS source exists (`QAPI.get_vehicle_trips :14470`, Vehicle GPS table with imei) for a true position. Threshold: red when minutes_out > 1.5 x median for that farm [?]. Remember "arrived" = first bucket shelved, so an arrived-but-not-unloaded truck still counts as en route.

### 4.4 Arrived but not shelved (age)
```sql
SELECT t.name, t.vehicle, t.arrived_at, TIMESTAMPDIFF(MINUTE, t.arrived_at, NOW()) mins_since_arrival,
       SUM(IFNULL(b.shelved,0)=0 AND IFNULL(b.off_truck,0)=0) unshelved, COUNT(*) carried
FROM `tabBucket Request Trip` t JOIN `tabBucket Request Trip Bucket` b ON b.parent=t.name
WHERE t.status IN ('Dispatched','Received') AND t.arrived_at IS NOT NULL AND t.trip_date >= CURDATE()-INTERVAL 7 DAY
GROUP BY t.name HAVING unshelved>0;
```
Per bucket (PLI) alternative: `pli.arrived_at IS NOT NULL AND pli.shelved=0`. Threshold: amber > 30 min after arrival, red > 60 min (proposed); at the hub the shelving screen blocks a bucket that is "not transferred" (`hub_shelving_block :7593`).

### 4.5 Orders blocked by transfer, with delivery date and team position
```sql
SELECT o.name opl, o.team, ps.schedule_date, pso.sequence, so.delivery_date, o.docstatus,
       COUNT(DISTINCT UPPER(pli.bucket)) buckets_moving, SUM(pli.stock_qty) stems_moving,
       MAX(pli.in_transit) any_on_road, MAX(pli.loaded_in_trolley) any_in_trolley
FROM `tabOrder Pick List` o
JOIN `tabSales Order` so ON so.name=o.sales_order
JOIN `tabPick List Item` pli ON pli.parent=o.name AND pli.parenttype='Order Pick List'
LEFT JOIN `tabPackhouse Schedule Order` pso ON pso.order_pick_list=o.name
LEFT JOIN `tabPackhouse Schedule` ps ON ps.name=pso.parent
WHERE o.docstatus=0 AND (pli.awaiting_transfer=1 OR pli.loaded_in_trolley=1 OR pli.in_transit=1)
  AND IFNULL(pli.shelved,0)=0 AND IFNULL(pli.not_found,0)=0
GROUP BY o.name, o.team, ps.schedule_date, pso.sequence, so.delivery_date, o.docstatus
ORDER BY so.delivery_date, ps.team, pso.sequence;
```
Team blocked behind a transfer: for each (team, schedule_date) the lowest `sequence` among rows returned = what blocks the later sequence numbers (when out-of-sequence bypass is off). Check the bypass first: `Production Settings.allow_issue_out_of_sequence`, `allow_pack_out_of_sequence` (both 1 locally). Delivery pressure: `delivery_date - CURDATE()`; threshold: red when delivery <= tomorrow and not yet in a trip or truck not left by early afternoon; amber when delivery tomorrow and trip released but not arrived by 12:00 (proposed).

### 4.6 Transfer lead-time trend (production-only, uses events)
```sql
SELECT DATE(e0.event_time) day, e0.farm,
  ROUND(AVG(TIMESTAMPDIFF(MINUTE, e0.event_time, e1.event_time))) trolley_to_truck_min,
  ROUND(AVG(TIMESTAMPDIFF(MINUTE, e1.event_time, e2.event_time))) truck_to_left_min,
  ROUND(AVG(TIMESTAMPDIFF(MINUTE, e2.event_time, e3.event_time))) left_to_shelved_min
FROM `tabBucket Transfer Event` e0
JOIN `tabBucket Transfer Event` e1 ON e1.bucket=e0.bucket AND e1.stage='Loaded on truck' AND e1.outcome='Done' AND e1.event_time>=e0.event_time
JOIN `tabBucket Transfer Event` e2 ON e2.bucket=e0.bucket AND e2.stage='Left the farm' AND e2.outcome='Done' AND e2.event_time>=e1.event_time
JOIN `tabBucket Transfer Event` e3 ON e3.bucket=e0.bucket AND e3.stage='Shelved at sales farm' AND e3.outcome='Done' AND e3.event_time>=e2.event_time
WHERE e0.stage IN ('On trolley','Loaded in trolley') AND e0.outcome='Done'
GROUP BY day, e0.farm;
```
Per-trip view: `requested_at -> released_at -> dispatched_at -> arrived_at -> received_at`. Bucket ids are REUSED each harvest (`STEPS.CYCLE_DAYS=21`): always restrict joins to events within about 21 days of the PLI `creation`, else multi-cycle buckets join wrongly. Buckets with `Bucket Reuse Anomaly` (Skipped Transfer) have no complete chain.

### 4.7 Other cards
* Planner health: `AT.status :56` (`enabled`, `frequency`, `auto_dispatch`, `last_run` with `waiting`, `unreachable`, `failed`, `filling`).
* Left-behind buckets: `TS._left_behind :430` logic (trip Order `buckets > loaded_buckets` on a trip that left that farm).
* Anomalies: `Bucket Transfer Event WHERE stage='Anomaly: left overnight' AND event_time >= CURDATE() - INTERVAL 1 DAY`.
* Idle buckets in transfer: `rt_inactive.get_inactive_buckets` (idle >= `inactive_bucket_days`, default 4).

---

## 5. Who to call for each stage (real roles / fields)

| Stage | Person / role | Field or role to use |
|---|---|---|
| Not allocated to a trip / no truck planned | Assistant Packhouse Manager (44 enabled users locally) | `Bucket Request Trip.owner` (auto trips are created by whoever's save/cron ran: often Administrator), `Order Pick List.owner/modified_by`, trip `requested_by` |
| Truck requested, not released | Bucket logistics coordinator at Kapkolia | `Bucket Request Trip.requested_by/requested_at`; no named role exists (any app user can release); fallback Assistant Packhouse Manager |
| Truck released, not at farm / driver | Driver | `Bucket Request Trip.vehicle` -> `Vehicle.employee` (NULL for every local vehicle: needs filling); `Bucket Request Trip.released_by` |
| Loading at the remote farm (slow loading, bucket missing, short) | Farm cold-store attendant / Farm Manager | `Bucket Request Trip Bucket.loaded_by`, PLI `loaded_in_trolley_by` / `loaded_on_truck_by` / `left_farm_by` / `not_found_by`; role `Farm Manager <farm>` (Torongo 8, Simotwo 8, Chepsito 7, Kaptumbo 6 enabled users locally), `Coldstore Attendant` (4 users); `Farm.farm_manager/farm_approver/unit_manager` are EMPTY on every farm |
| Truck left, late arriving | Driver + coordinator | `Bucket Request Trip.dispatched_at/last_departed_at`, `heading_to`; GPS via Vehicle if wired |
| Arrived, not shelved | Hub shelving staff: Stock User (223) incl. Coldstore Attendant; Farm Manager Kapkolia (7) | PLI `arrived_by`, `shelved_by`; trip `arrived_by` |
| Issuing waits / team blocked | Issuer / Packing Supervisor (129) / team line supervisor | `Order Pick List.team`, `Pick List Item.issued_by`; `Packing Teams` has no lead field |
Notes: `Bucket Request Trip` has DocPerm only for System Manager; the apps work through whitelisted methods with `ignore_permissions`. `Bucket Transfer Event` readable by Stock Manager / Stock User. Several shifts: "who is on shift" is not modelled.

---

## 6. Uncertainties and data gaps
1. Which PLI `*_at` stamps are populated on production is unknown. Locally only `arrived_at`, `shelved_at`, `issued_at` exist and they are back-filled at the row's `modified` (all 210 rows with both shelved and issued stamps have identical timestamps). `loaded_in_trolley_at`, `loaded_on_truck_at`, `left_farm_at` are NULL everywhere locally.
2. Event-to-stage mapping gap: `saveTrolleyData` logs "On trolley" but `STEPS.STAGE_STEP` maps only "Loaded in trolley", so `loaded_in_trolley_at` for trolley scans made through that path is filled by `reconcile` (row `modified`, up to 5 min late and possibly overwritten timing) rather than by the event. A plain Dispatch press logs no "Left the farm" event. [C]
3. "Arrived" is inferred from shelving or manual confirm: there is no independent arrival time; truck-at-gate to shelved delay is invisible. A GPS table (`QAPI.get_vehicle_trips`) exists but its ingestion on production is unverified.
4. No ETA, no route-window enforcement alert, no driver on Vehicle locally (`Vehicle.employee` NULL), no customer priority, no named transfer-dispatcher role.
5. Local trip/BTE history is only 3 synthetic JTEST trips; all lead times, per-farm trip counts, truck utilisation must be recomputed on production. Local `Bucket Logistics Route` = 0, so the auto-planner cannot run locally (no routed truck); `KDT 177A` carries `custom_dispatch_truck=1`, which excludes it as a transfer truck.
6. Sequence-block settings (`allow_issue_out_of_sequence`, `allow_pack_out_of_sequence`) are 1 locally; production values determine whether a late truck blocks a whole team (2.1 point 3) or only reorders the queue (2.1 point 4).
7. Row `modified` as issue time (section 2.3) includes later edits (packing steps, replacement). Hour distributions are indicative of the compression pattern, not exact.
8. Shared buckets (one physical bucket serving several orders, `shared_buckets :311`) and reused bucket ids mean bucket counts per trip use `trip_numbers :3406`; never count PLI rows as buckets.
9. `fetchAllocatedBuckets` window is delivery [today, today+1]; an order delivering today whose buckets are still at the farm is on the list, but `AT._place_scheduled` plans only orders delivering after today: a same-day late order relies on a human/ASAP path. [C]
10. Transfers that cross a day (truck out past midnight) are force-ended at 00:00 by `close_previous_day_trips`, so a late arrival around midnight may show "Not shelved when trip ended" even if shelving happens minutes later.
