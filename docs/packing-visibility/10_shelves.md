# 10. Shelves: how shelf capacity and shelf clearing affect packing

Scope: read-only research (local site `kaitet.local`, no writes). Paths: `P` = `apps/upande_packhouse/upande_packhouse`, `Q` = `apps/upande_quality/upande_quality`.
Tags: **[V]** verified in code or by a local query; **[?]** uncertain / not provable locally.

## 0. Data caveats (read first)
- **The local snapshot is frozen at about 2026-10-13** (today is 2026-10-25). Receivings, shelvings and almost every shelf row stop at 10-13 (last 20 stray rows on 10-23). Any age measured against `CURDATE()` is inflated by 12 days. All ages below use an as-of date of `2026-10-13` unless stated. Counts are a one-month sample (Shelving Log starts 2026-09-19), not a season.
- `Cold Store Capacity` table **does not exist locally**, so capacity = registered Shelf rows x 2 for every farm (`capacity_source = "Registered shelves"`). Production may use explicit `max_buckets`/`max_shelves` **[?]**.
- Role `Shelf Clearer` exists; **0 users hold it locally**. `Coldstore Attendant` 4 enabled users, `Stock User` 223, `Stock Manager` 58, `Packing Supervisor` 129, `Assistant Packhouse Manager` 44. Production staffing **[?]**.
- `Production Settings` local values: `discard_age` 5.0, `max_shelving_age` 1, `allow_shelving_in_transit` 1, `allow_hub_shelving_without_transfer` 1, `transfer_hub_farm` Kapkolia, `amber_time` 3. `carry_over_unshelved_buckets` is **not a column locally** (reads None; code at `P/api/remote_transfer/transfer_scheduling.py:1709`). Shelf Locations: 6 enabled farms, all `max_allocation_age` 4, `cooling_hours` 0; `sales_shelf`=1 only for Kapkolia and Karen.
- The mobile shelving UI (what the shelver literally sees) is **not in the repos I could read**: `/home/jk/Projects/upande-packhouse` only has the Karen tenant screens (packing/issuing/logistics); `createShelvingEntry`, `clearShelfItem`, `getShelfItemsForClearing` are called by a different client. I can only document server responses (`reason` codes) **[?]**.
- "Ravine" does not appear as a farm locally. The six farms are Kapkolia (hub, sales shelf), Karen (sales shelf, small), Torongo, Simotwo, Chepsito, Kaptumbo (remote, no sales shelf).

## 1. Rules that decide whether a bucket CAN be shelved

Two implementations of the same endpoint exist. `createShelvingEntry` is a DB **Server Script** ("Shelving Entry", API, enabled, 30 KB) which Frappe serves before the Python function of the same name; the Python port is `Q/mobile/api.py:createShelvingEntry:1367`. `P/mobile/api.py:shelveBucket:5518` is a third variant (v16 multi-variety). Server-script text was not diffed line by line **[?]**.

| # | Rule | Where | Refusal `reason` |
|---|---|---|---|
| 1 | shelf_id, bucket_id, farm required | `Q:1384-1403`, `P:5538-5545` | `shelf_id_not_null`, `bucket_id_not_null`, `farm_not_null` |
| 2 | Shelf does not need to exist: an unknown shelf id is **auto-created** (no farm yet; farm set from the app's farm at first shelving) | `Q:1408-1415`, `P:5558-5565`, `P:5770` | none: never refused |
| 3 | Not already on this shelf | `Q:1466-1475`, `P:5567-5570` | `duplicate_entry` |
| 4 | Not already on another shelf, unless it is a transfer bucket (draft OPL row flagged awaiting/in transit/trolley): then the stale remote shelf row is deleted ("self-heal") | `Q:1479-1530`, `P:5572-5598`, delete at `P:5833-5840` | `duplicate_entry` |
| 5 | **Max 2 distinct buckets per shelf** (distinct, not rows; a bucket of 3 varieties is still 1) | `Q:1535-1549`, `P:5599-5604`; same rule in shelf-to-shelf `transferBucket` `Q:12815-12828` | `two_buckets_per_shelf` ("The shelf is full.") |
| 6 | Mixed varieties on one shelf: **no rule** (1,578 of 3,536 in-use shelves hold >1 variety locally) | no check exists | n/a |
| 7 | Must have a Receiving / Late Receipt entry | `P:5606-5622` | `not_received` |
| 8 | Harvest->receiving gap <= 1 day | `P:5635-5672` | `no_matching_harvest`, `harvest_receiving_gap_too_large` |
| 9 | Receiving not stale: <=40 days (Kapkolia-origin 50) | `P:5683`, `Q:1972` | `stale_receiving_date` |
| 10 | **`max_shelving_age`** (Production Settings, local = 1 day): harvest older than that cannot be shelved. Present in `Q:2005-2030` only (not in `P:shelveBucket`) | `Q:2010` | `harvest_too_old` |
| 11 | A shelf belongs to its farm: shelving onto it counts at the shelf's farm whatever the app farm is; never refused, logged as "Shelving farm corrected" | `Q:1420-1456`, `P:5707-5725` | none |
| 12 | Bucket already trucked out of its remote farm cannot be shelved on a remote shelf; must be shelved at the sales farm | `remote_shelving_block` `P/api/remote_transfer/transfer_scheduling.py:7536` | `already_transferred` |
| 13 | At the hub: bucket still waiting at a remote farm (never loaded) cannot be hub-shelved, unless `allow_hub_shelving_without_transfer` (local 1 = lifted) | `hub_shelving_block` `:7593` (setting read `:7604`) | `not_transferred` |
| 14 | `allow_shelving_in_transit` (local 1): a transfer bucket shelved back at its remote farm "returns to waiting" instead of being refused | `allow_shelving_in_transit :7489`, `return_to_farm :7497`, `remote_shelving_block :7561` | none when 1 |
| 15 | Farm of a shelf must match for shelf-to-shelf moves | `Q:12780-12795` | `cross_farm_not_allowed` |
| 16 | Transfer bucket mid-truck cannot be moved shelf-to-shelf | `Q:12797-12812` | `mid_truck_transfer` |

- Carry-over: `carry_over_unshelved_buckets` (Production Settings) controls what happens to buckets a trip carried but nobody shelved when the truck starts a later run: if on they ride the truck's next trip (`_carry_unshelved :1721`, `_end_trip_for_newer_run :1782`), otherwise the trip stays open until they are shelved, End Trip, or midnight close (`close_previous_day_trips :1865`, `keep_as_anomaly :2048`). It is a trip rule, not a shelf rule.
- Nothing in code says "shelf must be registered", "shelf must be free of a different variety", or "farm of bucket = farm of shelf". Registration is soft: an unregistered shelf is created on the fly (rule 2).
- **What the shelver sees when shelves are full**: the server only returns `status failed / two_buckets_per_shelf / "The shelf is full."` for that shelf. There is no server-side "next free shelf" suggestion. The only endpoint that lists shelf contents is `getShelfItemsForClearing` (Shelf Clearer only). The queue of waiting buckets is the Cold Room v2 "Received, not shelved" figure (below) **[V]** (server), UI behaviour of the mobile app **[?]**.
- **Workarounds the rules permit (inferred, not observed on site)**: (a) scan the bucket onto any unregistered code (auto-created, counts at the app's farm); (b) double up beyond 2 via paths that do not enforce rule 5; locally 264 shelves hold 3 buckets and 338 hold 4 (602 shelves = 17% of in-use shelves are over capacity), e.g. Simotwo 178, Chepsito 176, Torongo 139, Kaptumbo 99; (c) leave buckets in trolleys / at receiving unshelved (queue below). **[?]** which path creates the 3-4 bucket shelves.

## 2. How a full cold room blocks the chain

1. **Received-not-shelved list**: `P/api/v2/cold_room.py:_not_shelved:141` (latest Receiving/Late Receipt of a bucket with no later cycle entry, no live Shelf Item, no Shelving Log, no onward stock entry, not issued). Rendered with the capacity block: `cold_room_capacity.build:117` compares `incoming` with free slots (`incoming_exceeds_free`), shown by `www/cold-room-v2.html:342` ("N buckets received, waiting for a shelf: more than the X free slots, not all can be shelved").
2. **Allocation needs a shelf**: availability is `Shelf Item` INNER JOIN `Shelf` WHERE `s.farm IN (enabled Shelf Locations farms)` AND age < `discard_age` AND age <= farm `max_allocation_age` AND hours-on-shelf >= `cooling_hours`, and bucket not on an open Discard Request (`P/upande_packhouse/page/sales_allocation/sales_allocation.py:690-760`, `DISCARD_EXCLUSION :27`). A received-not-shelved bucket, a bucket on a shelf of a disabled/NULL farm, or a bucket older than 4 days is **invisible to the allocator**. [V]
3. **Transfers**: a remote bucket is only "arrived" when it is shelved at the hub (`_shelve_update_transit_status`, `auto_arrive_for_bucket :5826`, `auto_receive_trucks_for_bucket :1247`). A truck that has arrived but whose buckets cannot be shelved keeps its Bucket Request Trip open (Dispatched/arrived with unshelved). `end_shelved_trips` (5-min scheduler) only ends a trip when all buckets are shelved/issued/off-truck. [V]
4. **OPLs stay draft**: `opl_submit_blockers` (`sales_allocation.py:2245`) blocks while any row is `awaiting_transfer`, `in_transit` or (`loaded_in_trolley` and not `shelved`). The last shelving scan at the hub submits the OPL (`Q:check_and_submit_opl :1766`, `P:_shelve_check_submit_opl :6031`, `_try_submit_opl_if_complete :2353`). So every unshelved arrival is a draft order that cannot be packed. [V] Local sample: 4 draft OPLs / 22 rows blocked this way (21 awaiting, 1 in transit, 1 trolley-unshelved); 1 Dispatched trip with 1 unshelved bucket; 0 trips arrived-with-unshelved right now.
5. **Shelf-to-shelf transfer and offline issuing need a shelf too** (`not_on_shelf` at `Q:12754`, `Q:12980`). Issuing itself (`P:issueBucketToSaleOrderItem :4878`) does not refuse a bucket with no shelf row; it just deletes whatever rows exist (`P:5120-5215`) **[V]** so ghost-allocated buckets can still be issued if physically present.
6. Local magnitude of the queue (as of 10-13, `_not_shelved` over all dates): 2,745 buckets: Simotwo 926, Kapkolia 600, Chepsito 468, Karen 299, Kaptumbo 245, Torongo 198, no farm 9. By age: 0-1 day 1,149 (42%); 2-7 days 834; 8-30 days 233; >30 days 529 (data artefacts: buckets received but never shelved and never closed). `incoming > free` is false in total (2,745 vs 3,474 free) but true per farm for Chepsito (468 vs 0 free) **[V]** and Simotwo (926 vs 148 free: 1,306 capacity less 1,158 occupied).

## 3. How clearing shelves affects packing

**Clearing paths** (all delete the `Shelf Item`, close the `Shelving Log` row with a reason; none touches Bucket Allocation Status or the pick rows):

| Path | Code | Log reason | Guard on allocated stock |
|---|---|---|---|
| `clearShelfItem` Server Script (and `getShelfItemsForClearing` to list), "emergency, role-gated": needs role **Shelf Clearer** or Administrator | DB Server Script, no repo code | `Shelf Cleared` (creates a closed log row if none was open) | **none**: does not check allocations; does not touch BAS |
| Replacement of a missing bucket (not found) | `sales_allocation._clear_shelf_item :3129`, call `:3404`; New bucket cleared `:3508` | `Shelf Cleared` (old), `Replaced` (new) | clears the old bucket's row only if nothing else allocated from it (`:3395-3410`) |
| Issuing (`issueBucketToSaleOrderItem`), `packing_reject :431` | `P/mobile/api.py:5182-5210` | `Issued to Sales Order` | normal |
| Offline issuing | `Q:12932-13030` | `Offline Issuing` | **refused if allocated** (`bucket_allocated`) |
| Transfer to truck | `Q:9380-9395`, `P:saveTrolleyData` | `Transferred (Trolley/Truck)` | normal |
| Discard approved (`createDiscardEntry`) | `Q:700-727` | removes the row; **no log reason written** (`Discarded` / `Aged Out` are never written anywhere) | bypasses age/allocation check on an approved list |
| Reconciliation script `clear_stale_shelf_items` | `P/api/remote_transfer/transfer_scheduling.py:7265` (dry_run default) | `Transferred (Trolley/Truck)` or `Issued to Sales Order` | n/a |

- **Who clears**: only Shelf Clearer (clearShelfItem) is a deliberate "clear". The log's `shelved_by` is the ORIGINAL shelver, not the clearer, and `removed_on` is set with `update_modified=False`, so **the clearer's identity is not recorded** in Shelving Log. `Deleted Document` has only 134 Shelf Item rows (Administrator 91, judah 38, james 2, a.rotich 2, wilfred 1) because most deletions bypass it.
- **No auto-clearing by `discard_age`**. `discard_age` (5) and `max_allocation_age` (4) only hide old stock from allocation (`stock.bucket_rows :142 too_old`, `sales_allocation.py:736-745`). No scheduler removes Shelf Items (hooks has none). Stale rows stay on the shelf and keep consuming capacity until a Discard Request is approved and each bucket is discarded. **Local: 13 Discard Requests (all Approved) cover 3,802 bucket rows; only 3 are marked discarded.** [V]
- **Shelf Cleared is the dominant removal locally and it is bulk, not an exception**: 21,119 log rows (Shelf Cleared) vs 6,367 open shelved, 3,276 offline, 2,963 issued, 2,062 trolley/truck, 304 replaced. They occur in only **234 distinct timestamps** (e.g. 1,901 rows in the single minute 10-05 18:15; 1,659 at 10-13 07:49), mostly in two daily windows (07-08h ~9,200 rows, 15-18h ~10,900 rows), i.e. a batch process, not single scans. The batch mechanism is not in the repos (Server Script or console) **[?]**. 17,894 (85%) of cleared buckets are shelved again later; 5,236 had a new Harvesting entry between shelving and 1 day after the clear (bucket-id reuse clean-up). So most clears are "make the shelf row of a reused/old bucket go away so the shelf is free again" **[?] inferred**.
- **Ghost allocations (allocated, no shelf)** local, base = submitted OPL rows, not issued/offline/not-found, bucket set: 2,802 rows / 557 OPLs / 198,290 stems; **ghost = 1,295 rows / 462 OPLs / 86,542 stems** (46% of rows). Classification by the bucket's Shelving Log: **1,106 rows (417 OPLs, 74,404 stems) had a `Shelf Cleared` AFTER the allocation was created**; 41 issued/offline-issued after allocation; 89 never in the log; 46 removed before allocation; 13 other. By farm of the pick row: Kapkolia 1,132 rows, Torongo 71, Karen 49, Simotwo 23, Chepsito 18. All ghost OPLs are dated 09-04..10-11 (older than 7 days at snapshot) so a good share is bookkeeping debris (physically packed but never flagged) rather than live blocked orders **[?]**. 128 of 462 ghost OPLs have nothing but ghost rows left. Over-allocation side: 121 BAS rows (11,840 stems) carry live allocation lines with no shelf item.
- **Clears that hit allocated stock**: 4,453 of 21,119 clears (21%) removed a bucket that still had an unissued pick row created before the clear; per day on 10-04..10-13: 444, 464, 34, 446, 555, 6, 1,096, 10, 534, 742. Effect: the order keeps its pick row and its Packing Guide, the allocator no longer sees the stock (so it will not re-allocate it), the issuing screen still lists the bucket and the packer finds an empty spot. Fix path = replace bucket (`replaceRequestedBucket`) or re-shelve.
- **Clear-then-reshelve**: a re-shelved bucket opens a new Shelf Item and a new log row; BAS `shelf_location` is only updated by `_shelve_update_bas :6011`. The allocation survives only if BAS rows survive (not cancelled), which clearShelfItem does not do, so a re-shelved bucket becomes "allocated and on shelf" again by itself **[V code]**.
- **Reason taxonomy** (Shelving Log.reason options): Shelved, Issued to Sales Order, Issued from Coldstore, Transferred (Trolley/Truck), Discarded, Replaced, Shelf Cleared, Aged Out, Transferred (Shelf-to-Shelf), Offline Issuing. Written by code: all except Discarded, Aged Out, Issued from Coldstore.

## 4. Capacity facts (local, as of the snapshot)

`api/v2/cold_room_capacity.py`: `BUCKETS_PER_SHELF = 2` (`:34`), `CAPACITY_LEVELS` (`:36-40`): **>=100% "Full / over capacity" (key full), >=90% "Almost full" (almost), >=80% "Getting full" (getting)**, else normal; unknown if capacity is 0. Capacity = Cold Store Capacity.max_buckets, else max_shelves x 2, else registered shelves x 2 (`capacity_for :95`). Occupied = distinct buckets on a shelf from `stock.bucket_rows` (held and allocated buckets still occupy).

| Farm | Registered shelves | Capacity (buckets) | Occupied | % | Level | Shelves full / over(>2) / partial / empty | Incoming (not shelved) | Shelves in use |
|---|---|---|---|---|---|---|---|---|
| Chepsito | 683 | 1,366 | 1,406 | **102.9** | full (over by 40) | 361 / 176 / 79 / 67 | 468 | 616 |
| Simotwo | 653 | 1,306 | 1,158 | 88.7 | getting (just under 90) | 242 / 178 / 103 / 130 | 926 | 523 |
| Torongo | 818 | 1,636 | 1,249 | 76.3 | normal | 326 / 139 / 161 / 192 | 198 | 626 |
| Kaptumbo | 627 | 1,254 | 950 | 75.8 | normal | 283 / 99 / 105 / 140 | 245 | 487 |
| Kapkolia (hub) | 1,753 | 3,506 | 2,000 | 57.0 | normal | 912 / 0 / 177 / 664 | 600 | 1,089 |
| Karen | 370 | 740 | 339 | 45.8 | normal | 128 / 10 / 57 / 175 | 299 | 195 |
| Total (6 farms + test + no-farm) | 4,918 | 9,836 | 6,362 distinct | 64.7 | normal | 2,252 / 602 / 682 / 1,382 | 2,745 | 3,536 |

- Test farms and no-farm shelves inflate the registered count (6 "Test Shelf Ops Farm", 1 "... 2", 7 with no farm). Shelf name prefixes: KPK/BLK (Kapkolia), TRG, CHP, SMT, KPT. Karen has 126 shelves named KPT-* and 103 BLK-*; **92 Karen-origin buckets sit on Kaptumbo-registered shelves** (counted in Kaptumbo's capacity; wrong-register example), 3 Kapkolia<->Karen crossovers, 3 on Chepsito with no item farm.
- **At >=90% / 100%**: there is no hard stop in code at any percentage: shelving only checks 2 buckets on THAT shelf (rule 5) and creates shelves on demand. The capacity levels are a **display** (alert list `alerts`, `worst`), not a gate. Practical effect at Chepsito (102.9%, 468 more arriving): no free slot, so received buckets queue; 176 shelves already hold 3-4 buckets (legacy / bypassed rule). So "100%" means workers double up or leave buckets unshelved, which then are not allocatable (section 2).
- **Capacity is mostly dead stock**: as of 10-13, buckets on shelf aged >=5 days (discard_age) = Chepsito 72%, Simotwo 62%, Kapkolia 62%, Kaptumbo 59%, Torongo 56%, Karen 54%; aged >=14 days: Chepsito 1,009 of 1,406, Kapkolia 894 of 2,001, Simotwo 565, Torongo 574, Kaptumbo 413, Karen 131. Buckets sitting on a not-rejected, not-discarded Discard Request (snapshot-date, local live): Chepsito 1,159 (82% of its occupied), Kapkolia 1,130, Torongo 729, Simotwo 758, Kaptumbo 609, Karen 169 = 4,554. **If those were discarded, Chepsito would be about 18% full (247 buckets)** [V arithmetic, assumes the same shelf rows]. Healthy buckets (<=4 days) per farm: Chepsito 390, Kapkolia 771, Simotwo 440, Torongo 548, Kaptumbo 393, Karen 158.
- Flow facts (Shelving Log, 09-21..10-23, distinct buckets shelved per active day: average / peak): Chepsito 404 / 754, Kapkolia 894 / 1,785, Torongo 342 / 637, Simotwo 230 / 552, Kaptumbo 141 / 489, Karen 117 / 275. Receiving entries per day: Chepsito 362 / 450, Simotwo 435 / 719, Torongo 309 / 388, Kaptumbo 233 / 275, Kapkolia 231 / 940, Karen 111 / 135. Healthy dwell (shelved to issue/transfer): Kapkolia issue 9.6 h avg, offline 11.6 h; remote trolley/truck 16-30 h; max seen 2-8 days.
- **Peak-season sizing (Valentine's)**. Assumptions: the shelf is a 2-bucket slot; required shelves = peak-day buckets in x multiplier k x dwell days / 2; baseline peak day from the sample above (a busy but not a Valentine day); dwell 1.5 days (observed average 10-30 h plus slack) or 4 days (the `max_allocation_age`, i.e. stock allowed to sit until the last allocatable day); remote farms keep ALL buckets until trucked; no stale stock is assumed. Registered shelves: 4,904 for the 6 farms.

| Scenario | Shelves needed (all 6 farms) | Extra vs 4,904 (sum of deficits) | Hot farms (extra shelves) |
|---|---|---|---|
| 1x, dwell 1.5 d | 3,371 | 0 | none |
| 2x, dwell 1.5 d | 6,740 | **1,836** (about 3,670 buckets) | Kapkolia 925, Chepsito 448, Simotwo 175, Torongo 138, Kaptumbo 107, Karen 43 |
| 3x, dwell 1.5 d | 10,110 | **5,206** (about 10,400 buckets) | Kapkolia 2,264, Chepsito 1,014, Torongo 616, Simotwo 589, Kaptumbo 474, Karen 249 |
| 1x, dwell 4 d | 8,984 | 4,080 | all farms; Kapkolia 1,817, Chepsito 825 |
| 2x, dwell 4 d | 17,968 | 13,064 | |
| 3x, dwell 4 d | 26,952 | 22,048 | |
| Alt: today's occupied buckets x k (assumes the stale mix stays) | 2x: +2,229 shelves; 3x: +5,750 | | Chepsito +723 / +1,426 at 2x / 3x |

  Reading: at 1x with healthy turnover the system has slack (3,371 needed of 4,904), but only if stale stock is gone. At 2x the hub (Kapkolia) and Chepsito run out first; the hub is the pinch point because remote buckets are shelved there on arrival (Kapkolia peak 1,785 shelved/day). Every day of dwell beyond 1.5 days multiplies the need (4 days = 2.7x). **Uncertain**: peak dwell, how much of Kapkolia's 1,785 are arrivals shelved twice, and production Cold Store Capacity overrides.

## 5. Taxonomy of shelf-caused packing delays and detection (thresholds proposed **[?]**)

| Code | Delay | Detection (local run) | Amber / Red | Who to call |
|---|---|---|---|---|
| SH1 | Store full / almost full | `cold_room_capacity.build` `pct`, `level` (>=80 / >=90 / >=100); Chepsito 102.9 | amber >=90% (almost), red >=100% or `incoming_exceeds_free` | farm manager (`Farm Manager <farm>` roles exist for all 6) and Coldstore Attendant of that farm; hub: `Farm Manager Kapkolia` |
| SH2 | Unshelved queue (received, no shelf) | `cold_room._not_shelved` per farm: count and age; 2,745 locally | amber queue > 10% of free slots or oldest > 4 h; red > 12 h or queue > free | Coldstore Attendant / shelf supervisor; Stock User (223) is the only broad shelving-like role **[?]** |
| SH3 | Arrived not shelved (trip) | `Bucket Request Trip` arrived with unshelved buckets, or PLI `arrived_at` set, `shelved=0`; trip query in `07_transfers.md 4.4`; 0 now | amber > 30 min, red > 60 min | hub shelver, Packing Supervisor, `Farm Manager Kapkolia` |
| SH4 | OPL draft held by unshelved transfer | `opl_submit_blockers` text; SQL below; 4 OPLs / 22 rows | red if delivery <= 1 day | Assistant Packhouse Manager (44 users) |
| SH5 | Ghost allocation | SQL G1 below; 1,295 rows / 462 OPLs | amber any unissued ghost on an OPL delivering <= 2 days; red <= 1 day | allocator; Shelf Clearer / Stock Manager to explain the clear; Quality Manager if on discard list |
| SH6 | Shelf clear hit allocated stock | SQL G2 below; 4,453 clears hit allocated stock (21%) | amber >= 1 in last 24 h, red >= 10 or >5% of clears | Shelf Clearer role holders (0 users locally), Stock Manager (58) |
| SH7 | Dead stock holds slots | buckets aged >= `discard_age` still on shelf / on Approved-not-discarded request; SQL G3 | amber > 25% of occupied, red > 50% (all farms exceed 50% locally) | Quality Manager (45), Stock Manager, Discard Request approver (`approved_by`) |
| SH8 | Wrong or unknown shelf | shelf farm null; Shelf Item.farm <> Shelf.farm (101 rows locally: 92 Karen buckets on Kaptumbo shelves); shelf over capacity (602 shelves) | any | shelf supervisor / farm manager |
| SH9 | Hub vs remote imbalance | compare `pct` of Kapkolia vs remote farms; Kapkolia incoming vs free | red if hub >= 90% while remote queues exist | `Farm Manager Kapkolia` |

## 6. Dashboard metrics with SQL (all run read-only locally; `:asof` = `CURDATE()` live)

M1 Capacity by farm (live): reuse `cold_room_capacity.build` (call `get_cold_room`) to keep one definition. Equivalent SQL for shelves by state:
```sql
SELECT s.farm, COUNT(*) registered,
       SUM(x.n=2) full_, SUM(x.n>2) over_, SUM(x.n=1) half_, SUM(x.n IS NULL) empty_
FROM `tabShelf` s
LEFT JOIN (SELECT parent, COUNT(DISTINCT bucket_id) n FROM `tabShelf Item`
           WHERE stem_qty>0 AND IFNULL(bucket_id,'')<>'' AND IFNULL(variety,'')<>'' GROUP BY parent) x ON x.parent=s.name
GROUP BY s.farm;   -- occupied buckets = SUM(x.n); capacity = registered*2; pct = occupied/capacity
```
M2 Unshelved queue (count/oldest): call `cold_room._not_shelved(sc)`; group `farm`, `DATEDIFF(CURDATE(), received_on)`. Local result above.

M3 Draft OPLs held by unshelved/transfer:
```sql
SELECT COUNT(DISTINCT o.name) opls, COUNT(*) rows_
FROM `tabOrder Pick List` o JOIN `tabPick List Item` p ON p.parent=o.name AND p.parenttype='Order Pick List'
WHERE o.docstatus=0 AND (IFNULL(p.awaiting_transfer,0)=1 OR IFNULL(p.in_transit,0)=1
      OR (IFNULL(p.loaded_in_trolley,0)=1 AND IFNULL(p.shelved,0)=0));   -- 4 / 22
```
M4 Arrived not shelved: `SELECT COUNT(*), MIN(p.arrived_at) FROM tabPick List Item p JOIN Order Pick List o ON o.name=p.parent AND o.docstatus<2 WHERE p.arrived_at IS NOT NULL AND IFNULL(p.shelved,0)=0 AND IFNULL(p.issued,0)=0 AND IFNULL(p.not_found,0)=0` (0 locally).

G1 Ghost allocations (M5):
```sql
SELECT IFNULL(NULLIF(p.farm,''),'-') farm, COUNT(*) rows_, COUNT(DISTINCT p.parent) opls, ROUND(SUM(p.stock_qty)) stems
FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name=p.parent AND o.docstatus=1
WHERE p.parenttype='Order Pick List' AND IFNULL(p.bucket,'')<>'' AND IFNULL(p.issued,0)=0
  AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0
  AND IFNULL(p.in_transit,0)=0 AND IFNULL(p.awaiting_transfer,0)=0 AND IFNULL(p.loaded_in_trolley,0)=0
  AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE UPPER(si.bucket_id)=UPPER(p.bucket) AND si.stem_qty>0)
GROUP BY 1;   -- total 1,295 / 462 / 86,542 locally
```
Add a column `cleared_after_alloc` via `EXISTS (SELECT 1 FROM tabShelving Log l WHERE UPPER(l.bucket_id)=UPPER(p.bucket) AND l.reason='Shelf Cleared' AND l.removed_on>=p.creation)` (1,106 rows). Restrict to `delivery_date`/`o.date_created` in the dashboard window.

G2 Clears in the last N hours that hit allocated stock (M6):
```sql
SELECT COUNT(*) clears,
       SUM(EXISTS (SELECT 1 FROM `tabPick List Item` p JOIN `tabOrder Pick List` o ON o.name=p.parent AND o.docstatus<2
                   WHERE p.parenttype='Order Pick List' AND UPPER(p.bucket)=UPPER(l.bucket_id) AND p.creation<l.removed_on
                     AND IFNULL(p.issued,0)=0 AND IFNULL(p.issued_offline,0)=0 AND IFNULL(p.not_found,0)=0)) hit_allocated
FROM `tabShelving Log` l
WHERE l.reason='Shelf Cleared' AND l.removed_on >= NOW() - INTERVAL 24 HOUR;
```
Run per day locally: 10-10: 3,681 clears / 1,096 hit; 10-13: 3,137 / 742. All time 21,119 / 4,453. (Window 24 h is empty locally because the snapshot is frozen.)

M7 Clears per day by reason: `SELECT DATE(removed_on), reason, COUNT(*) FROM tabShelving Log WHERE removed_on IS NOT NULL GROUP BY 1,2` (shows Shelf Cleared batches).

G3 Dead stock on shelves (M8):
```sql
SELECT farm, COUNT(*) buckets, SUM(a>=5) ge_discard_age, SUM(a>=14) ge14,
       ROUND(SUM(a>=5)/COUNT(*)*100,1) pct_stale
FROM (SELECT s.farm, si.bucket_id, MIN(DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added))) a
      FROM `tabShelf Item` si JOIN `tabShelf` s ON s.name=si.parent
      WHERE si.stem_qty>0 AND IFNULL(si.bucket_id,'')<>'' GROUP BY s.farm, si.bucket_id) t GROUP BY farm;
```
(use the farm's `max_allocation_age` and `discard_age` from Shelf Locations / Production Settings, not hard-coded 5). On the discard list: join `Discard Request Bucket` (`discarded=0`) and `Discard Request.workflow_state<>'Rejected'`.

M9 Wrong shelf / unknown shelf: `Shelf Item.farm<>Shelf.farm` (101 rows), `Shelf.farm IS NULL` (7 shelves, 0 with stock), shelves with >2 buckets (602): `SELECT n,COUNT(*) FROM (SELECT parent, COUNT(DISTINCT bucket_id) n FROM tabShelf Item WHERE stem_qty>0 GROUP BY parent) t GROUP BY n`.

M10 Peak-risk gauge: `peak_buckets_per_day(farm) x multiplier x dwell_days / 2` vs registered shelves. Inputs: `SELECT farm, DATE(shelved_on) d, COUNT(DISTINCT bucket_id) FROM tabShelving Log GROUP BY farm,d`; dwell = `AVG(TIMESTAMPDIFF(HOUR, shelved_on, removed_on))` for reasons Issued/Offline/Transferred. Make the multiplier a slider.

M11 Hub vs remote: the same M1 split by `farm = transfer_hub_farm` vs others.

## 7. Findings to emphasize
1. Capacity pressure is mostly a **stale stock / clearing problem**, not a flow problem: 54-72% of occupied buckets are past `discard_age`, 4,554 sit on approved discard lists that are almost never executed (3 of 3,802 discarded). Chepsito's 102.9% would be about 18% without them.
2. **Shelf Cleared is a bulk, role-gated, allocation-blind operation**: 21,119 rows, 234 timestamps, 4,453 hit allocated stock, and clearing does not release the BAS allocation, hence 1,295 ghost pick rows (1,106 clear-after-allocation).
3. No hard capacity gate exists: full cold rooms show up as queues of unshelved buckets (2,745), draft OPLs and open trips, not as refusals.
4. The Shelf Clearer role has no users locally and the clearer is not recorded in the log: attribution for dashboard "who cleared" is not possible without a new field (Version/comment) **[?]**.
