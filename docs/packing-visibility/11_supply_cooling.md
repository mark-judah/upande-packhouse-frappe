# 11 - Supply side: harvest -> grading -> receiving -> shelving -> cooling (research for "Packing Visibility")

Scope: read-only. Local site `kaitet.local` is a frozen snapshot: Stock Entry data is dense to 2026-10-13 (then 19 stray rows on 10-23), only 1,196 OPLs / 305 Bucket Allocations / 6,065 Pick List Items, `Shelf Item` 7,831 rows. Timings measured locally are INDICATIVE (replayed / bulk-seeded data, see caveats section 6); every metric below comes with SQL for production.
Paths: `AGR` = `apps/upande_agriculture/upande_agriculture/mobile/api.py`, `QAPI` = `apps/upande_quality/upande_quality/mobile/api.py`, `PH` = `apps/upande_packhouse/upande_packhouse/`. Related: doc 01 (packing), 03 (OPL blockers), 07 (transfers), 08 (scheduler).

---------------------------------------------------------------------------------------------------

## 1. The supply pipeline (stages, code, stamps, typical duration)

There is NO `Harvesting` doctype and NO `Grading Repack Tracker` doctype in the bench (grep over all apps: none; `tabHarvesting` does not exist locally). Harvest, grading and receiving are all **Stock Entries** distinguished by `stock_entry_type` (counts locally, submitted: Harvesting 1.71 M, Grading 1.33 M, Receiving 507 k, Receiving Quarantined 114, Quarantine Accept 57, Quarantine Rejects 5). Bucket state lives in `Bucket QR Code` (`status` Available / In Use, `last_stock_entry`, `current_journey_start`).

| # | Stage | Code | Record / stamp | Notes | Typical time (local, indicative) |
|---|-------|------|----------------|-------|------|
| 1 | Harvest (field scan: farm, greenhouse, harvester, bucket, variety, length, qty) | `AGR createHarvestStockEntry :479` | Stock Entry type `Harvesting` (posting_date = harvest date, `creation` = real moment); bucket -> `In Use` (`:~610`) | Cap per bucket (Production Settings > Harvest Item Group Config): Standard Roses 200, Spray Roses 100 (local rows). Enforced by `validate_bucket_stem_limit :62`, summed over TODAY's Harvesting entries of that bucket (posting_date = today). A Standard bucket can only be harvested again after receiving (blocked "already in use"). Spray roses are rejected on this form (`item_group_is_under ... "Spray Roses"`); they are harvested in the grading scan. | Harvest scans 08:00-17:00 (peak 10:00-12:00 and 14:00-15:00, Oct 12-13). Most local Standard rows are exactly 200 (avg 190 for Chepsito/Kaptumbo), i.e. the cap IS the typical bucket. |
| 2 | Grading (bunch scan: bunch QR, graded_by, length, bunch size) | `AGR createGradingStockEntry :246` | Stock Entry type `Grading`, `custom_bunch_id`, `custom_graded_by`, `custom_grading_date` (date only), `custom_scanned_grading=1`; for sprays also makes the `Harvesting` entry (grading IS the harvest, `:~300-345`) | `Scan Location Mapping` `<farm>-MAP` row action `Grading` gives source warehouse (`Simotwo/Chepsito Receiving Cold Store - KR` locally); target = the greenhouse warehouse sent by the app. Packing refuses a bunch with no Grading entry: `PH mobile/api.py :1060` and `:3131` ("This bunch has not been graded in the system. Perform the grading scan on it to enable packing"). | Locally the first Grading entry of a bucket is 0-0.5 h after harvest for Simotwo/Torongo/Karen (graded BEFORE the receiving scan in >97% of buckets on 2026-10-08) and ~3.4 h for Kapkolia. Grading is concentrated 10:00-12:00 and 14:00-15:00. [UNCERTAIN: Standard vs Spray order is mixed; 80% of local rows are spray]. |
| 3 | Receiving into the farm Receiving Cold Store | `QAPI createReceivingStockEntry :905` (target `"<farm> Receiving Cold Store - <abbr>"` `:1198`) | Stock Entry type `Receiving` (claims ALL unclaimed Harvesting entries of the bucket's current journey via `custom_receiving_entry` on the harvest rows; bucket -> `Available`; `custom_harvest_date`, `custom_graded_by`, `custom_grading_date` copied) | **`posting_date` is back-dated to the harvest date when the gap is >=1 day (`:1204`), so only `creation` is the real receive moment** (same warning in `apps/upande_agriculture/upande_agriculture/api/v2/receiving.py` header; that page already measures harvested = received + short + awaiting, `LATE_HOURS = 24`). | Harvest -> receiving: 0.4-1.0 h average per farm (Chepsito 0.46, Kapkolia 0.76, Kaptumbo 0.39, Karen 0.54, Simotwo 0.99, Torongo 1.03; max 5.5 h; Oct 12-13 sample, 10,600 buckets). Receiving scans 09:00-17:00. 91 of 2,136 buckets harvested 2026-10-08 had no receiving link (awaiting / short). |
| 3b | Quarantine at receiving | `Receiving Quarantined` SE (QAPI `:3646`, `:7435`, `:9688`: warehouse "<farm> Receiving Quarantined - KR"), then `Quarantine Accept` (back to store) or `Quarantine Rejects` (written off) | no farm on the quarantine entries; the receiving v2 page assigns the cycle's farm | Only 114 quarantines in 2 months locally (0.1% of receivings): not a material supply cause, but stems in quarantine cannot be shelved and are invisible to packing. | n/a |
| 4 | Shelving (scan bucket onto a shelf) | `QAPI createShelvingEntry :1367` (`Shelf Item.date_added = now_datetime() :2209`, `harvest_date = harvest posting_date :2215`; hub-side `shelveBucket` `:12842`) | `Shelf Item` rows (variety, length, qty, `date_added` datetime, `harvest_date` date, `receiving_date`, `grading_date`, `farm`, `bucket_id`, parent `Shelf` with `farm`) | Gates (`:1920-2040`): (a) a Harvesting entry must exist for receiving date or day before; (b) harvest -> receiving gap <= 1 day ("harvest_receiving_gap_too_large"); (c) receiving date not older than 40 d (50 for Kapkolia); (d) **`Production Settings.max_shelving_age` (local value = 1 day) caps harvest age at shelving ("harvest_too_old")**. | Local snapshot: harvest date -> shelving date lag: 0 d 13% of stems, 1 d 52%... of recent rows (31 k / 121 k / 77 k / 46 k / 33 k / 20 k / 21 k / 11 k stems for 0..7 d). Hence most shelf stock is shelved the NEXT day or later (see 6: local shelving timestamps are bulk-seeded, not reliable for hour-of-day). |
| 5 | Remote farm -> hub transfer | doc 07 (`Bucket Request Trip`, PLI `loaded_on_truck_at`, `left_farm_at`, `arrived_at`, `shelved_at`; arrival = first shelving at the hub) | PLI stamps | Hub = Production Settings `transfer_hub_farm` (local: Kapkolia). Sales shelves (`sales_shelf=1`): Kapkolia, Karen. `allow_hub_shelving_without_transfer=1`, `allow_shelving_in_transit=1`. Locally `left_farm_at` is NULL on all PLIs, so transfer lag cannot be measured here; only `arrived_at = shelved_at` (hours 8-18, peaks 10:00 and 13:00-14:00 and 17:00). Per-bucket local estimate (harvest creation -> first shelf date_added, window 3 d, bucket-id reuse makes it noisy): remote farm buckets reaching Kapkolia average ~40-53 h after the receiving scan (min 16 h), farm-local shelves 6-35 h. | see doc 07; production SQL in 5.4 |
| 6 | Cooling gate (see section 2) | `PH api/v2/core/stock.py :143`, `sales_allocation.py :742-752`, `:1112` | none stored: derived from `Shelf Item.date_added` | Default 0 h = no gate. | 0 h locally on every farm |
| 7 | Allocation (stock becomes an OPL) | `sales_allocation.get_sales_order_items_with_buckets :488` | `Bucket Allocations`, OPL | needs `available_qty`, age <= farm max age (4 d locally), age < discard age (5), cooling satisfied. | locally 120/125 allocations were made in the same clock hour as the shelving |

NOT to be confused: **Precooling** (`createPrecoolingEntry PH mobile/api.py :1729`, `Box Label.precooling_in_at`, `precooled_at`) is the BOX precooling room AFTER packing (`Pack -> Precooling -> Stage -> Load`). It has nothing to do with the stem cooling gate that holds shelved stock back before allocation. The dashboard must label them differently ("stem cooling" = supply; "box precooling" = outbound).

---------------------------------------------------------------------------------------------------

## 2. What "cooling" means in the data model

* Setting: **`Shelf Locations.cooling_hours`** (Float, default 0, description "Minimum hours a bucket must have been on this farm's shelf (since scanning-on) before it can be allocated. 0 = no cooling requirement."), a CHILD table of **Production Settings** (`shelf_locations`, one row per farm with `enabled`, `sales_shelf`, `max_allocation_age`, `cooling_hours`). Doctype JSON: `apps/upande_quality/upande_quality/upande_quality/doctype/shelf_locations/shelf_locations.json :14, :50-57` (modified 2026-09-15). It is not in Stock Settings and not a global value: per farm.
* Local values: all six enabled farms (Kapkolia, Chepsito, Simotwo, Torongo, Kaptumbo, Karen) have `cooling_hours = 0.0` and `max_allocation_age = 4.0`; `discard_age 5`, `amber_time 3`, `max_shelving_age 1`, `takt_time 2.5`, `hub_buckets_per_shelf 2`. The INTENDED production value is 4 h (per brief; not in any fixture or patch in the repo: grep for cooling_hours shows only the doctype JSON, `_get_production_config`, stock.py and the allocator). [CONFIRM the live value per farm with `SELECT farm, enabled, cooling_hours, max_allocation_age FROM \`tabShelf Locations\` WHERE parent='Production Settings'`.]
* Read by:
  * `PH upande_packhouse/page/sales_allocation/sales_allocation.py _get_production_config :60-127` (`cooling_hours: float(row.get("cooling_hours") or 0)` at `:97`; `.get()` because the column can be missing on some sites; farms that are NOT enabled rows get no farm_config entry, hence no cooling and default max age 5).
  * Allocator filter `:742-752`: `hours_since_shelved = TIMESTAMPDIFF(HOUR, si.date_added, now)` `:703`; buckets with `hours < cooling_hours` are dropped from the candidate list. Measured from `date_added` (scan onto the shelf), NOT from harvest or receiving.
  * Reason chip `:1082`, `:1112-1119`: "Still cooling since shelving" / "Shelved Nh ago - <farm> requires Xh cooling, Yh left" (priority below pending_discard and remote_farm, above discard-age). Only fires if `cooling_hours > 0`.
  * v2 core `PH api/v2/core/stock.py :27, :92, :143-144`: `cooling = hours_on_shelf < farm cooling_hours`; `allocatable = 0 if (too_old or cooling) else free`. Consumed by Stock Visibility (`stock_visibility.py :18, :407` `too_old_or_cooling = free - allocatable`), Avails, Allocation Planning, Cold Room. `AVAILABLE_DEFINITION :46` says available = not held, not allocated, not too old, not cooling.
* Hours are integer-floor `TIMESTAMPDIFF(HOUR ...)`: a bucket shelved at 08:55 passes a 4 h gate at 12:55 exactly (hour boundary), no minute rounding up. `hours_on_shelf` uses MIN(date_added) of the (bucket, variety, length) group.
* **Consequence of cooling_hours = 0**: no gate at all. Stock is allocatable the minute it is shelved; `cooling` is always false; the `cooling` reason can never appear; Stock Visibility's `too_old_or_cooling` collapses to "too old". Locally 120 of 125 uncancelled allocations were created in the same hour as the shelving (0 h), confirming the gate is inactive. If production has 4 h, then stock shelved at 09:30 cannot be allocated before ~13:30 (hour floor) and a "waiting for cooling" classification is meaningful; the packing team sees the line short during that window even though the stock is physically on the shelf. Note the gate only affects ALLOCATION (OPL creation), not an already-created OPL: packing of orders allocated earlier is unaffected.
* Age definition (`age_days`): `DATEDIFF(CURDATE(), MIN(COALESCE(harvest_date, DATE(date_added))))` (`stock.py :91`, allocator `:699`): calendar days since the harvest posting date, else since shelving date; ignores time of day. **0 d = harvested today** (same calendar day as the query/allocation); 1 d = harvested yesterday. Because allocation happens a day before delivery date and shelving typically the next morning (section 1), true 0-day allocations are possible only for stock harvested AND shelved the same day (Karen / Simotwo-type local farms) and allocated that afternoon. `too_old = age > farm max_allocation_age (4) or age >= discard_age (5)`; `amber` from `amber_time` (3 d) `:882`. Cold Room v2 uses a different age: `(today - received_on)` (`cold_room.py :188`) - do not mix.
  * Local current (as of snapshot) 0-day allocation share: of 125 dated allocations, 125 had age_at_alloc = 0 (all harvested the day they were allocated: synthetic seeding), so the local value of "0-day allocation share" is 100% and not representative.

---------------------------------------------------------------------------------------------------

## 3. Daily volumes (local, Oct 1-12) and supply vs demand

Harvested stems per day (Stock Entry Detail qty, all farms, Harvesting, posting_date): 250 k, 265 k, 244 k, 269 k, 278 k, 264 k, 296 k, 310 k, 270 k, 187 k, 295 k, 253 k (Oct 1..12); 97-116 varieties; spray rows 80% of rows (cap 100 stems) but Standard rows carry ~65% of stems (Intermediate 138 k, Premium 49 k, Retail 42 k on Oct 8). Farm split Oct 8: Simotwo 102 k, Chepsito 65 k, Torongo 56 k, Kaptumbo 51 k, Kapkolia 27 k, Karen 8 k (Kaptumbo/Chepsito mean 190 stems per entry = full 200 buckets; Simotwo 24 = sprays). Length mix Oct 8: 52 cm 136 k, 42 cm 91 k, 62 cm 57 k, 72 cm 21 k, 82 cm 3 k.
OPL demand per packing day (delivery_date - 1; `SUM(Pick List Item.stock_qty)` = stems, `qty` is bunches): Oct 1..11 = 41 k, 45 k, 23 k, 8 k, 44 k, 65 k, 74 k, 30 k, 51 k, 65 k, 25 k stems over 25-104 variety-length pairs. So local total harvest is 4-30x the OPL demand: **volume is never the constraint locally; mix and timing are** (the local copy only has 1,196 of the OPLs; ratio is a floor). In production the same comparison must be done per (variety, length), not in total.
Forecast pages: `Production Forecast` (submittable; one per greenhouse + variety; child `Production Forecast Week`: week_number, iso_year, grade, `budget_stems`, `manual_budget_stems`, `revised_forecast_stems`, `actual_stems`, variances; 117 forecasts / 1,770 week rows locally) and `Production Projection` (4 rows) in `apps/upande_agriculture`; `budget.py` (`generate_budget :116`, `grid_payload :451`, `budget_by_grade :200`, `revise_forecast :250`) and the desk page `production_budget`. Forecast is WEEKLY by greenhouse/variety/GRADE (grade ~ length class; `grade_of :61`), NOT daily by length: it can say "variety X will be short this week", not "today at 07:00". Use it for the weekly trend panel only; for "is a harvest coming today" use harvest-to-date plus yesterday's same-weekday pattern (no per-day harvest plan exists in the code base).
Bucket cap check: Standard `max qty per entry = 200`, spray 100 (local `MAX(qty)`); breaches of the cap cannot exist (validated at scan and in `amendHarvestEntry`).

---------------------------------------------------------------------------------------------------

## 4. The "packing waits for supply" signals (what can be detected)

| Signal | Definition | Source | Reliability |
|--------|------------|--------|-------------|
| S1 Short line, shelf empty, harvest expected | For the packing day D: stems still to pack for (variety, length) > shelved allocatable stems, and variety is harvested today (greenhouse/farm harvest to date, or last-week same-weekday average) | OPL Pick List Item not issued vs `core/stock.available_cells()`; harvest SQL 5.2 | good for "shelf empty"; "expected" is heuristic (no daily harvest plan) |
| S2 Stock harvested but not received | Harvest buckets with `custom_receiving_entry` NULL (awaiting receiving) | `receiving.py` (identity harvested = received + short + awaiting); SQL 5.3 | good; bucket reuse handled there (UPPER(TRIM), LOOKBACK 3 d) |
| S3 Received but not shelved | Receiving entry `creation` with no `Shelf Item.bucket_id` added after it (bucket-case-insensitive) | Stock Entry Receiving + Shelf Item; SQL 5.3 | good; remote-farm buckets shelved at the hub are "in transfer", not "not shelved" (use PLI `in_transit/awaiting_transfer`, doc 07) |
| S4 Stock cooling | `free` stems with `hours_on_shelf < cooling_hours` | `core/stock.bucket_rows` field `cooling` | only meaningful if production `cooling_hours > 0`; expose `hours_left` like `sales_allocation.py :1118` |
| S5 Quarantine hold | `Receiving Quarantined` not yet accepted/rejected | receiving.py `QUAR_IN` logic | low volume |
| S6 Supply gap | harvested (variety, length, day) < ordered (same, packing day) | SQL 5.5 | needs same-grain keys; Standard length from `Stock Entry.custom_stem_length`, Pick List Item `stem_length` |
| S7 Early idle vs first shelving | team first-issue/pack time vs first `Shelf Item.date_added` of the day for the lines it needs | PLI `issued_at` (local first issue 08:20-10:30), Shelf Item `date_added`, FPL | local shelving times are not valid; use production |
| S8 Pre-gate at shelving | buckets refused "harvest_too_old / gap_too_large / stale" | `QAPI :1920-2040`; error log "Max Shelving Age Validation Failed" etc. | those buckets can never become supply; they are written to Error Log only (no doctype) [UNCERTAIN whether Bucket Reuse Anomaly captures them] |

Classification for a short (variety, length) at time t (apply in this order, first match wins; this is the dashboard's "waiting for ..."):
1. `shelved_available >= to_pack` -> **Covered** (not waiting for supply).
2. `cooling_stems >= shortfall` -> **Waiting for cooling** (stock on shelf, gate not open; ETA = min `cooling_hours - hours_on_shelf`).
3. `received_not_shelved_stems >= shortfall` (stock in Receiving Cold Store, or in transit to the hub) -> **Waiting for shelving** (for remote farms: "waiting for truck / hub shelving", doc 07).
4. `harvested_not_received_stems >= shortfall` -> **Waiting for receiving/grading** (grading missing = bunch gate `mobile/api.py :1060`).
5. else if variety/length harvested so far today < expected -> **Waiting for harvest**; else **No supply / true shortage** (needs allocator / sales action; also links to bucket replacement, doc 06).
Allow partial coverage: allocate the shortfall across buckets 2->3->4->5 in that order and show the split.

---------------------------------------------------------------------------------------------------

## 5. Dashboard metrics and production SQL

Common: `:day` = packing day, `:now` = now. All stems queries use bounded `creation`/`posting_date` ranges (Stock Entry is 1.7 M rows locally: always add `se.posting_date BETWEEN ...` and `se.stock_entry_type = ...`). Run EXPLAIN first on production; `Stock Entry.creation` has no dedicated index in this repo [UNCERTAIN]; filter on posting_date (indexed in ERPNext) and bound with creation additionally.

### 5.1 Supply coverage ratio per variety/length (the headline)
Prefer the Python core (`core/stock.available_cells(...)`, already the one definition of available) and add the pick-list side:
```sql
-- stems still to pack for packing day D (delivery_date = D+1), per variety/length
SELECT pli.item_code AS variety, pli.stem_length,
       SUM(pli.stock_qty) AS to_pack_stems
FROM `tabOrder Pick List` opl
JOIN `tabSales Order` so ON so.name = opl.sales_order
JOIN `tabPick List Item` pli ON pli.parent = opl.name
WHERE so.delivery_date = DATE_ADD(%(day)s, INTERVAL 1 DAY)
  AND opl.docstatus < 2
  AND IFNULL(pli.issued,0) = 0 AND IFNULL(pli.not_found,0) = 0
GROUP BY pli.item_code, pli.stem_length;
```
(`stock_qty` is stems: Bunch(10) qty 7.6 -> stock_qty 76.2 locally. "Still to pack" is approximated by "not yet issued"; packed-but-not-issued does not exist, issue precedes pack. Orders with no team may be hidden from the app but still count as demand; decide with ops. Align the filter with `core/pipeline.py` ordered/issued stems rather than re-inventing it. Karen-type own-farm orders ship from their own shelf.)
Shelved available (per variety, length, farm, with cooling flag), no Python, same grain as `bucket_rows`:
```sql
SELECT si.variety, si.stem_length, s.farm,
       SUM(si.stem_qty) AS shelved_stems,
       SUM(IF(TIMESTAMPDIFF(HOUR, si.date_added, NOW()) < IFNULL(sl.cooling_hours,0), si.stem_qty, 0)) AS cooling_stems,
       MIN(si.date_added) AS first_shelved, MAX(si.date_added) AS last_shelved
FROM `tabShelf Item` si
JOIN `tabShelf` s ON s.name = si.parent
LEFT JOIN `tabShelf Locations` sl ON sl.parent='Production Settings' AND sl.farm = s.farm AND sl.enabled = 1
WHERE si.stem_qty > 0 AND DATEDIFF(CURDATE(), COALESCE(si.harvest_date, DATE(si.date_added))) <= IFNULL(sl.max_allocation_age, 5)
GROUP BY si.variety, si.stem_length, s.farm;
```
(approximation: free = shelved - allocated is NOT subtracted here; subtract `Bucket Allocation Status.allocated_quantity` per `(bucket_id, item_code, stem_length)` as `stock.py :104-117` does, or call `core/stock`. The 31 buckets that sit on two farms' shelves locally must follow the MIN(s.farm) rule `stock.py :60-66`.)
`coverage = (shelved_stems - allocated_elsewhere - cooling_stems) / to_pack_stems`; colours: >=1 covered, 0.5-1 amber, <0.5 red; report also stems_short = max(0, to_pack - available).

### 5.2 Harvest today / hourly by variety, length, farm
```sql
SELECT se.farm, sed.item_code AS variety, se.custom_stem_length AS stem_length,
       HOUR(se.creation) AS hr, COUNT(DISTINCT se.name) AS entries, SUM(sed.qty) AS stems
FROM `tabStock Entry` se
JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
WHERE se.stock_entry_type = 'Harvesting' AND se.docstatus = 1
  AND se.posting_date = %(day)s AND se.creation >= %(day)s
GROUP BY se.farm, sed.item_code, se.custom_stem_length, HOUR(se.creation);
```
(Spray rows come via the Grading scan: `custom_stem_length` set there too; Standard rows have length also on the child `sed.custom_stem_length` in Receiving. Harvest SE `custom_stem_length` is parent-level.)

### 5.3 Pipeline stage times and the "stuck" counts
```sql
-- harvest -> receiving per bucket (real times = creation)
SELECT h.farm, COUNT(*) buckets,
       ROUND(AVG(TIMESTAMPDIFF(MINUTE, h.creation, r.creation))/60, 2) AS avg_h_to_recv_h,
       MAX(TIMESTAMPDIFF(MINUTE, h.creation, r.creation))/60 AS max_h
FROM `tabStock Entry` h JOIN `tabStock Entry` r ON r.name = h.custom_receiving_entry
WHERE h.stock_entry_type='Harvesting' AND h.docstatus=1 AND h.posting_date BETWEEN %(from)s AND %(to)s
GROUP BY h.farm;
-- harvested, not yet received (awaiting receiving), today
SELECT h.farm, SUM(sed.qty) stems, MIN(h.creation) oldest, COUNT(DISTINCT h.custom_bucket_id) buckets
FROM `tabStock Entry` h JOIN `tabStock Entry Detail` sed ON sed.parent=h.name
WHERE h.stock_entry_type='Harvesting' AND h.docstatus=1 AND h.posting_date >= DATE_SUB(CURDATE(), INTERVAL 2 DAY)
  AND h.custom_receiving_entry IS NULL GROUP BY h.farm;
-- received, not shelved (use receiving.py logic; cheap approximation by bucket)
SELECT r.farm, COUNT(DISTINCT r.custom_bucket_id) buckets, SUM(sed.qty) stems, MIN(r.creation) oldest_recv
FROM `tabStock Entry` r JOIN `tabStock Entry Detail` sed ON sed.parent=r.name
WHERE r.stock_entry_type='Receiving' AND r.docstatus=1 AND r.creation >= DATE_SUB(NOW(), INTERVAL 3 DAY)
  AND NOT EXISTS (SELECT 1 FROM `tabShelf Item` si WHERE si.bucket_id = r.custom_bucket_id AND si.date_added >= r.creation)
GROUP BY r.farm;
-- receiving -> shelving per bucket (bucket ids are reused: bound by 3 days as the code does LOOKBACK_DAYS=3)
```
Spray: grade-in-place, so "received but not graded" is the leading gap (spray bunches are harvested while grading). Missing grading = no `Grading` SE with `custom_bunch_id`; the receiving entry copies `custom_grading_date`/`custom_graded_by` from the bucket's Grading entry, so NULL there = bucket received ungraded: `SELECT COUNT(*) FROM tabStock Entry WHERE stock_entry_type='Receiving' AND custom_grading_date IS NULL AND creation>=...` [UNCERTAIN for Standard roses, where bunching is repack work and may follow receiving].

### 5.4 Remote farm transfer lag (hub supply)
```sql
SELECT origin_warehouse, DATE(loaded_on_truck_at) d,
       AVG(TIMESTAMPDIFF(MINUTE, loaded_on_truck_at, left_farm_at))/60 AS load_to_leave_h,
       AVG(TIMESTAMPDIFF(MINUTE, left_farm_at, arrived_at))/60 AS leave_to_arrive_h,
       AVG(TIMESTAMPDIFF(MINUTE, arrived_at, shelved_at))/60 AS arrive_to_shelved_h
FROM `tabPick List Item` WHERE left_farm_at IS NOT NULL AND shelved_at >= %(from)s GROUP BY 1,2;
```
`arrived_at` = first shelving of the trip unless pressed (doc 07 section 1.1): arrive->shelved is therefore ~0 by construction; true unloading delay is invisible. Local: `left_farm_at` all NULL, `arrived_at == shelved_at`.

### 5.5 Harvest vs demand gap (trend)
Join 5.2 (per variety/length/day, summed over farms) to 5.1 (per packing day D, delivery D+1) on (variety, length, D). Day-level gap = harvested(D-1 and D) - to_pack(D); production packs D from stock harvested D-1 (shelved D morning) plus D. Use a rolling 14-day trend of `harvest_stems / ordered_stems` per variety group (Production Forecast week rows for the weekly view: `actual_stems` vs `budget_stems`).

### 5.6 First shelving vs first packing per day
```sql
SELECT d.day, d.first_shelved, p.first_issue, p.first_pack
FROM (SELECT DATE(date_added) day, MIN(TIME(date_added)) first_shelved FROM `tabShelf Item` WHERE date_added >= %(from)s GROUP BY 1) d
LEFT JOIN (SELECT DATE(issued_at) day, MIN(TIME(issued_at)) first_issue, NULL first_pack FROM `tabPick List Item` WHERE issued_at >= %(from)s GROUP BY 1) p USING (day);
```
First pack = earliest Box Label / FPL creation (doc 01 [PROD]). Per team add `opl.team` and the farms the team's lines need (hub vs own-farm). Idle gap = first_issue(team) - first_shelved(farm of its lines) when negative-or-small means supply was ready; a team's first-issue minus the time of the first shelving of ANY line it needs is the "waited for shelf" minutes.

### 5.7 0-day allocation share and cooling effectiveness
```sql
SELECT DATE(ba.creation) d, DATEDIFF(DATE(ba.creation), x.hd) AS age_at_alloc, COUNT(*) allocs, SUM(ba.quantity_allocated) stems
FROM `tabBucket Allocations` ba
JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
JOIN (SELECT bucket_id, variety, MIN(harvest_date) hd FROM `tabShelf Item` GROUP BY bucket_id, variety) x
  ON x.bucket_id = bas.bucket_id AND x.variety = bas.item_code
WHERE ba.cancelled = 0 AND ba.creation >= %(from)s GROUP BY 1,2;
```
(Shelf Item rows disappear when stock is issued, so this is only valid for CURRENT shelf rows; for history use `Bucket Allocation Status` + the Shelving Log / Receiving `custom_harvest_date`. [UNCERTAIN]) A rising 0-day share with `cooling_hours = 4` means the gate is bypassed or set to 0 on that farm.

---------------------------------------------------------------------------------------------------

## 6. Caveats / uncertain

* Local snapshot: Stock Entry `posting_date` and `creation` are consistent from Sept 2 to Oct 13 (real-looking replay: harvest 08-17 h, receiving 09-17 h), but **Shelf Item `date_added` has bulk-seeded batches (e.g. 79.7 k-80.7 k s = ~22:15 on Oct 8 for four farms) and `Pick List Item.left_farm_at` is empty**, so local first-shelving hours, transfer lags and the "shelving peaks 08-10 h" pattern in section 1 are NOT trustworthy; only harvest/receiving/grading hourly patterns and the harvest->receiving durations are credible. All receiving->shelving durations (6-70 h) are ambiguous because bucket ids are reused and the join takes the latest receiving before the shelf row.
* Local demand is a thin copy (1,196 OPLs vs 1,793 submitted Sales Orders); the 4-30x harvest-to-demand ratio is not production.
* Cooling gate local = 0 h on every farm; production intended 4 h is NOT in code or fixtures: confirm. Hours are integer-floor.
* Cooling is applied to allocation only. A dashboard "waiting for cooling" is true only when the gate stops an allocation the packers need; if the OPL already exists the team can pack whatever has been issued.
* `age_days` ignores time of day; 0 d = harvested today. `Cold Room` uses days since received (different).
* No `Grading Repack Tracker`, no `Harvesting` doctype, no daily harvest plan; "harvest expected today" is inferred (yesterday/last-week pattern, or weekly Production Forecast).
* Max shelving age (1 d) and gap rules reject old buckets with an Error Log only; stock that would be supply never appears in Shelf Item. Count these from Error Log titles ("Max Shelving Age Validation Failed", "Harvest-to-Receiving Gap Validation Failed", "Stale Receiving Date Validation Failed") on production [UNCERTAIN on retention].
* Variety-length keys: Pick List Item `stem_length` and Shelf Item `stem_length` are strings like '52cm'; Standard 'Premium' variety grouping may differ between item_code and base variety (`budget.base_variety :56`).

---------------------------------------------------------------------------------------------------

## 7. Who to call

| Cause (classification) | Role / person | Where in system |
|-------|-------|-------|
| Waiting for harvest (not yet cut or short) | Farm Manager (per farm role: Farm Manager Simotwo/Torongo/Chepsito/Kaptumbo/Kapkolia/Karen: 38 users locally), farm greenhouse/section harvest lead; the harvester is `Stock Entry.custom_harvester` (Employee) | Harvest scans by farm/greenhouse (5.2) |
| Waiting for grading | Grading Hall Supervisor / Post Harvest Supervisor / Section Head Post Harvest (Employees: Grader 112, Post Harvest Supervisor 3, Section Head Post Harvest 6, Grading Hall Supervisor 1); roles `Grading Qc` (30), `Quality Controller` (32) | `custom_graded_by` on Grading/Receiving entries |
| Waiting for receiving (Receiving Cold Store) | Cold Room Attendant (13 Employees) / role `Coldstore Attendant` (4 users) / Stock User at the farm | Receiving v2 page |
| Waiting for shelving / not on shelf | Shelving staff at the shelf farm (Stock User 223, Coldstore Attendant); at the hub the Bucket Logistics coordinator for trucks (doc 07: Assistant Packhouse Manager - Supply Chain, 1) | Remote Transfers / Bucket Logistics |
| Waiting for cooling | Packhouse Manager / Assistant Packhouse Manager (44 users) decides whether to relax `cooling_hours` (Production Settings); Cold Room Attendant confirms the room temperature | Production Settings > Shelf Locations |
| Quarantine hold | Quality Manager (45) / Quality Controller | Receiving Quarantined / Quarantine Accept / Rejects |
| True shortage (no stock anywhere) | Assistant Packhouse Manager (allocation / bucket replacement, doc 06) and sales/order admin | Sales Allocation page |

---------------------------------------------------------------------------------------------------

## 8. Recommendations for the dashboard
1. Reuse `core/stock.available_cells()` (AVAILABLE_DEFINITION `:46`) for the shelved/cooling/available split; reuse `receiving.py` for harvested-received-short-awaiting; reuse `core/pipeline.py` for demand/issued so numbers equal the other v2 pages.
2. Expose `cooling_hours` per farm in the dashboard header ("cooling 4 h / OFF"), and show `hours_left` (= `cooling_hours - hours_on_shelf`) for cooling stems.
3. Always show both clocks for a stuck bucket: real `creation` of harvest/receiving/shelving, not `posting_date` (back-dated receiving).
4. Treat "waiting for harvest" as unconfirmed unless a per-day harvest plan exists (none in the code); label it "no stock yet; farm X has harvested N of usual M stems by this hour" using last-7-same-weekday median of cumulative stems by hour (5.2).
5. Production checks before shipping: live `cooling_hours`, `max_shelving_age`, Error Log retention, index on `Stock Entry.creation`, and typical hub shelving hour (not measurable locally).
