# Movement (MV): Stem Movement, Bucket Journey, Bucket Logistics

Method: read every code path end to end, ran SELECT/EXPLAIN on kaitet.local, and ran the
read-only endpoints through a harness (`scratchpad/audit/mv/harness.py`). The harness blocks
`frappe.db.commit`, stubs `log_error`, counts and times every `frappe.db.sql` call and rolls back.
To replay a past day it sets `frappe.utils.today/now_datetime` and the MariaDB session
`timestamp`. Live data ends on 2026-09-25 and today is 2026-10-06, so "today" on the live board
is all zeros. Most proofs below replay **2026-09-24 20:00**. Raw outputs are in `scratchpad/audit/mv/*.json`.

---

## Page: Stem Movement (/stem-movement-v2) — API: `api/stem_movement.getPackhouseFlowBoard`, `getBucketTrace`, `searchBoxLabels`

The v2 page calls only these three. `getStemMovementData` and `getStemMovementBuckets` (lines 371–1298) are legacy and not called by v2.

### What the page shows and how it's computed (brief)
- **KPIs** (computed client-side in `www/stem-movement-v2.html:228-245`):
  - Harvested today = `totals.harvested`.
  - Dispatched today = `totals.dispatched`.
  - "Still in the packhouse" = Σ `buffer.live` over all 7 buffers.
  - "Shelf stock past 48 h" = Σ of the age bands with tone `bad` (48–72 h plus >72 h), shown "of" Σ of all bands.
- **8 gates** (`_flows`, `stem_movement.py:1356-1414`): one SUM query per gate per day. The data source differs by gate:
  - harvested and received: Stock Entry.
  - shelved: Receiving entries whose bucket has a Shelving Log row, keyed by receipt date.
  - issued and loaded: Pick List Item flags, keyed by **OPL.date_created**.
  - packed: FPL items, keyed by `DATE(fpl.creation)`.
  - staged: **Box Label Item.qty**, keyed by `bl.date`.
  - dispatched: Delivery Note Item `stock_qty`.
- **7 buffers**: each has a "live" SQL. Then opening = `max(0, live − in + out)` and a 7-day series is rebuilt backward from today's gates and clamped at 0 (`build_buf`, 1567-1608).
- **Shelf waterfall**:
  - From the Shelving Log: opening at 06:00, shelved in = `DATE(shelved_on)=today`, issued out and discarded = `DATE(removed_on)=today` classified by `reason LIKE`.
  - "now" comes from live Shelf Item; variance = now − ledger.
  - An hourly step series for hours 06–18, age bands, and discard reasons.
- **3-day matrix**: `day_flows` for the last 3 days. **Cohort**: today's bucketed harvest, followed forward with EXISTS subqueries.
- **Box trace**: Box Label doc, OPL header, a Dispatch Form lookup, the "origin greenhouses" block, then 5–8 queries per bucket for the journey. Packed stems and bunches come from Pick List Item rows.

### Findings
| ID | Severity | Type | Finding | Evidence (file:line + real-data proof) | Correct definition / fix direction |
|---|---|---|---|---|---|
| MV-1 | Critical | Integrity | **Box trace is completely broken.** `getBucketTrace` reads `consignee` from Order Pick List, and that column does not exist. Every box that has an OPL returns `success:false`. | `stem_movement.py:57-62` `frappe.get_all("Order Pick List", fields=["team","consignee","farm"])`. Ran on BOX-OPL-2026-00045-3 and BOX-OPL-2026-00047-1: both return `(1054, "Unknown column 'consignee' in 'SELECT'")`. `SHOW COLUMNS FROM tabOrder Pick List LIKE '%consign%'` returns nothing. | Drop the field, or read consignee from the Sales Order. Add a smoke test for the endpoint. |
| MV-2 | Critical | Math | **Packed stems/bunches in the trace are wrong in scope and in source.** (a) They sum *every* Pick List Item of the OPL that has any `custom_box_id`, not only this box's rows. (b) `custom_box_id` is the *planned* box (set at allocation, 376 of 383 rows), not what was actually packed. The label side shows bunches from Box Label Item, so the two sides never reconcile. | `stem_movement.py:142-176, 345-357`. BOX-OPL-2026-00045-3: Box Label = 1 bunch (Odilia). FPL-2026-00013 box 3 actually packed **10 stems**, closed with "End of day / cutoff". Packing Guide planned 300. Once MV-1 is fixed the code would show **900 stems / 90 bunches** (all 9 OPL buckets × 100) against "1 bunch on label". | Packed for a box = `SUM(Farm Packlist Item.stock_qty) WHERE parent=box_label.farm_pack_lis AND box_id=box_label.box_number`. Bunches = stems ÷ UOM factor. Planned = Packing Guide (box_number). Show plan / packed / label side by side. |
| MV-3 | High | Math | **"Origin — greenhouses" in the trace is unrelated to the box.** It aggregates the last 500 harvest lines of the box's *farm*, not the buckets in the box. | `stem_movement.py:87-136`: `WHERE se.stock_entry_type='Harvesting' AND se.farm=%(farm)s ORDER BY se.creation DESC LIMIT 500`. No bucket or date link to the box. EXPLAIN shows a full scan of `tabStock Entry Detail` (3,263,280 rows, filesort). | Aggregate the harvest entries of the box's own buckets (`custom_bucket_id IN journey buckets`), taking the latest cycle at or before the OPL date. |
| MV-4 | Critical | Math | **Unit mixing (bunches counted as stems).** The staged gate and the awaiting-stage, staged and on-truck buffers sum `Box Label Item.qty`, which is in **bunches** (uom "Bunch (10/15/5)"). The other gates and buffers are in stems. The "Still in the packhouse" KPI adds them all together, and `awaiting_stage` has stems flowing in and bunches as its level. | `stem_movement.py:1394-1398, 1460-1470`. Example: BOX-OPL-2026-00050-1 has pack_rate 600 and qty 60 (Bunch (10)). Replay 2026-09-24: awaiting_stage reads opening 0, +600 packed (stems), −0 staged, live **169** (bunches); staged 78 and on_truck 106 are also bunches. | Stems per box = Σ FPL `stock_qty` for that box_id (or `bi.qty × UOM factor`). Box buffers can also be shown as box counts (COUNT DISTINCT box label). Never sum bi.qty as stems. |
| MV-5 | Critical | Math | **The shelf ledger counts the 00:00–06:00 window twice.** Opening is taken at 06:00, but shelved/issued/discarded use `DATE(x)=today` from midnight. Anything shelved before 06:00 is in both opening and "shelved in". Removals before 06:00 are left out of opening and also subtracted again. | `stem_movement.py:1477-1492`. Replay 2026-09-24: all **991 shelvings (130,569 stems) happened before 06:00**. Opening = 131,467 (includes them), shelved in = 130,569, so reconciled = **261,986** against about 131,417 actually logged. On 2026-09-20, 9 of 21 issue-outs were before 06:00. | All flows over `[06:00, now)`: `shelved_on >= morning`, `removed_on >= morning`. Or move opening to 00:00. The identity opening + in − out = close must hold by construction. |
| MV-6 | Critical | Filter | **Confirmed: the shelf ledger ignores farm, variety and location filters.** Opening, shelved in, issued out, discarded, the hourly step, `buckets_in` and discard reasons are all unfiltered. "now" and the age bands *are* filtered. The variance and waterfall become meaningless when a filter is set. | `stem_movement.py:1477-1529, 1548-1555`. Shelving Log already carries `farm`, `variety` and `greenhouse`. Replay 2026-09-24 with farm=Karen: opening 131,467 / in 130,569 (all farms; Karen's whole log is only 9,250 stems) against now 12,790, so variance is **−249,196**. | Apply `_frag("farm","variety","greenhouse")` to every Shelving Log query. Greenhouse can filter shelf stock too, because Shelf Item and Shelving Log both carry `greenhouse`. |
| MV-7 | Critical | Integrity/KPI | **78% of shelf stock has no Shelving Log row**, so "Unreconciled" is permanently about +475k. The ">48 h" KPI says 100% of the shelf is old (stale or phantom rows at remote farms). | 3,871 Shelf Items with stock; **2,872 of them (475,039 stems) have no log** (farms Chepsito, Kaptumbo, Simotwo, Torongo; earliest 2026-08-25). Today: now 606,241, ledger 131,517, variance **+474,724**; KPI "606,241 past 48 h of 606,241". The Shelving Log only has Kapkolia and Karen rows. | Backfill a "Shelved" log row for each Shelf Item that has none, and require every shelving path to call `_write_shelved_log`. Find and fix the remote-farm shelving path (unverified which one). Then retire either the log or the live table as the source of truth. |
| MV-8 | High | Math | **Partial issues are invisible to the ledger, and fallback removal rows carry 0 stems.** A partial issue only lowers `Shelf Item.stem_qty`, and the log row keeps its original qty. A full removal flips the row and counts the *original shelved* qty as issued, on the day of the final draw. A log row inserted by the fallback path has no `stem_qty` and no `shelved_on`. | `mobile/api.py:4614-4618` (partial: no log), `4624-4648` (flip / fallback insert without stem_qty). Data: **45 "Issued to Sales Order" rows with stem_qty=0, shelved_on NULL**. Issued out on 2026-09-04/10/11/12/15/16 sums to 0 stems for 45 removals. | Make the Shelving Log an event ledger (one row per in or out movement with signed qty), or drive issued stems from the "Issuing From Cold Store" Stock Entries. |
| MV-9 | High | Math | **Removal reasons are misclassified.** The discard bucket is "anything not like Issue/Pick/Sales/Shelved", so it catches "Offline Issuing" ("Issuing" does not contain "Issue"), "Replaced" and "Transferred (Shelf-to-Shelf)". A shelf-to-shelf move is not an exit, and its new Shelved row is also counted as shelved in. | `stem_movement.py:1486, 1530-1536`. Replay 2026-09-24: discarded = 50, reason "Offline Issuing" (real issue). 2026-09-19: transfer 100 stems shown as discard. 2026-09-25: "Replaced" 200 shown as discard. | Use an explicit reason enum: issue = {Issued to Sales Order, Offline Issuing}, discard = {Discarded}, transfer = net-zero (exclude from both sides), replaced = its own reason. |
| MV-10 | High | Math | **The gates use dates that don't match their names.** "Issued" and "Loaded" are dated by `OPL.date_created`, not by when the issue or load happened. "Staged" uses `bl.date`. "Shelved" is a receipt-date cohort, while the shelf waterfall's "shelved in" is by `shelved_on`. The same day therefore shows two different "shelved" and "issued" numbers. | `stem_movement.py:1374-1404`. Replay 2026-09-24: gate issued = 154 but ledger issued out = 0 (plus 50 offline). Karen: gate shelved 4,560 vs ledger 130,569. | Issued = Stock Entry "Issuing From Cold Store" (`stock_movement.TYPE_ISSUING`) by posting date. Loaded = "Dispatch" SE or the box-label loaded timestamp. Staged needs a `staged_on` timestamp. Shelved = Shelving Log `shelved_on`. |
| MV-11 | High | Integrity | **`Pick List Item.custom_box_label` is never written** (0 of 383 rows). The pack_hall buffer ("issued, no box label") therefore never drains, and cohort "packed" is always 0. | `stem_movement.py:1453-1458, 1640`. Data: `SUM(custom_box_label<>'')=0`. Replay 2026-09-24: pack_hall live 2,414 stems, including buckets FPL-packed days earlier. | In the packhouse = issued stems − FPL packed stems per OPL and item (both in stems), or the Bin of the "Packhouse Store" stage warehouse. |
| MV-12 | High | Math | **Buffer identities don't close and the 7-day series is invented.** Opening = max(0, live − in + out), but in/out come from gates with a different scope from the live query. Example: in_transit covers bucketed harvests of the last 2 days, while its inflow is *all* harvests including the 4,029 unbucketed entries. The clamp hides the break. The series is rebuilt backward with the same mismatched flows. | `stem_movement.py:1567-1592`. Replay 2026-09-24: in_transit 0 + 172,004 − 131,809 ≠ live 90. On-shelf series7 jumps 475,826 → 606,241 in one day. | Opening = closing level at 06:00 (or 00:00) read from a ledger (SLE/Bin balance as of a datetime). Store daily snapshots for the 7-day series instead of rebuilding it. |
| MV-13 | Med | KPI | **"Still in the packhouse" counts buffers that are not in the packhouse.** It includes "From the field" (cut, not received) and "On the truck", and it mixes units (MV-4). | `stem-movement-v2.html:230`, `stem_movement.py:1636`. Replay 2026-09-24: 610,498 = 90 + 1,400 + 606,241 + 2,414 + 169 bunches + 78 bunches + 106 bunches. | Σ stem buffers from received through packed-not-dispatched, all in stems. Show field and truck separately. |
| MV-14 | Med | Math | **The cohort ("today's cut") only covers bucketed harvest lines**, so it disagrees with the Harvested KPI. Bucket reuse (35k QR codes against 1.6M harvests) lets a *later* cycle's receipt or shelving satisfy the EXISTS check, because there is no upper bound. | `stem_movement.py:1636-1650`. Replay 2026-09-24: KPI 172,004 vs cohort cut 131,809 (40,195 stems unbucketed). | Bound each EXISTS to the bucket's journey, e.g. `< next harvest of the same bucket` or by `Bucket QR Code.current_journey_start` / `custom_harvest_entry` links. |
| MV-15 | Med | Math | **"Dispatched" nets returns and is dated by the return.** The DN query has no `is_return=0`. | `stem_movement.py:1407-1410`. 2026-09-16: gross 1,790, return −200, shown **1,590**. 2026-09-17 shows −30. | `dn.is_return=0` for dispatched. Show returns as their own flow. |
| MV-16 | Med | Math | **The hourly shelf step only covers 06–18 h.** Events after 18:59 never appear, so the last point ≠ "On hand". | `stem_movement.py:1503-1517`. 2026-09-19: 21 shelvings / 1,840 stems at or after 19:00. | Run to `HOUR(now)`. Also clamp to the filters (MV-6). |
| MV-17 | Low | Math | Age bands use `COALESCE(receiving_date, date_added)`. `receiving_date` is a DATE (midnight), so ages are overstated by up to 24 h. The on-shelf level excludes blank varieties but the bands don't (0 rows today). | `stem_movement.py:1520-1529, 1440-1444`. | Use `date_added` (datetime) or Shelving Log `shelved_on`, with the same WHERE as the level. |
| MV-18 | Low | Filter | Farm filter uses `LIKE 'farm%'` and is matched to *warehouse names* for packed (`pli.source_warehouse`). Location only constrains upstream gates. The page warns about location, not about this. | `stem_movement.py:1349-1351, 1384`. | Filter by the warehouse→farm mapping (`SO Warehouse Mapping` / Warehouse.farm) with equality. |
| MV-19 | Low | Integrity | The trace swallows every sub-query error (`except: pass`) and re-runs identical queries as a "fallback" for receiving and grading. The `group` field reads `item_group`, which is never fetched, so it is always "". | `stem_movement.py:136-156, 223-293`. | Remove the duplicate queries, fetch `item_group`, and let errors surface. |

### Performance profile
- **getPackhouseFlowBoard: 86 SQL per load (84 from the endpoint itself). Measured 62.2 s (today), 69.6 s and 101 s (2026-09-24 replay, cold vs warm), 101 s with farm filter.**
  - About 69 s of 70 s is the three filter-option queries (`stem_movement.py:1664-1689`): DISTINCT item_code took 49.5 s, greenhouse 10.7 s, farm 8.8 s. They are a 60-day scan over Stock Entry (233,786 rows) joined to SED. They have no `docstatus` predicate, so the `(stock_entry_type, docstatus, posting_date)` index is only used on its type prefix (EXPLAIN rows ≈ 3.28 M, temporary + filesort).
  - `_flows` runs 8 queries × 8 calls = **64 queries**. `totals` repeats `day_flows[today]`, so 8 of them are pure duplicates. 7 days are fetched but only 3 are shown in the matrix (the others feed the invented series, MV-12).
  - Buffers 7, shelf 9, cohort 1: each is well under 0.3 s.
  - The "shelved" gate and the cohort use a correlated `EXISTS` per SE row. Shelving Log has no index on `shelved_on` / `removed_on`, and every filter is non-sargable `DATE(col)=d` (the table is small today: 1,076 rows).
  - Box Label and OPL have no date index (`bl.date`, `opl.date_created`). FPL has no index on `order_pick_list` / creation date.
- **getBucketTrace**: 6–17 queries before it dies (MV-1). Once fixed it is roughly 5 + 6–8 per bucket (N+1: harvest, receiving ×2, grading ×2, shelving, discard + detail), plus the greenhouse query, which is a **full scan of 3.26 M SED rows** (EXPLAIN `type=ALL`).
- searchBoxLabels: 1 query (LIKE '%q%' on name, 44 rows). Fine.

### v2 backend design notes
- **Source of truth per number**
  - **Harvested / received**: Stock Entry (types Harvesting; Receiving, Late Receipt), docstatus=1, `transfer_qty`, by posting_date (or posting_datetime for the 06:00 window).
  - **Shelved in / out**: the Shelving Log turned into an event ledger with signed qty, a reason enum, and every shelving path writing to it (fixes MV-7/8/9). Until then, on-shelf level = Σ Shelf Item `stem_qty`.
  - **Issued / packing / dispatch stage stock**: `stock_movement.py` now posts real legs:
    - Remote Transfers → Move To Graded Sold → Issuing From Cold Store → Packing → Dispatch.
    - Gate flows = SLE (or SE) sums by `stock_entry_type` and posting datetime.
    - Buffer levels = the **Bin `actual_qty` of each stage warehouse** (Receiving cold store, Graded Sold, Packhouse Store, Dispatch Coldroom, Delivery Truck), resolved per farm from `SO Warehouse Mapping`.
    - This is exact, filterable by item and warehouse, and opening = SLE balance as of 06:00.
  - **Packed**: FPL `pack_list_item.stock_qty` (stems), boxes = COUNT DISTINCT (FPL, box_id). Staged / loaded / dispatched boxes come from Box Label flags plus timestamps (add `staged_on` / `loaded_on`). Stems on labels = FPL stems for the box, never `bi.qty`.
  - **Box trace**: Box Label → FPL rows for the box (packed stems per variety) → Pick List Item rows for that box_id (buckets) → one query per table with `IN (buckets)` (mirrors `bucket_journey.getBucketJourney`).
- **One-query plan**:
  - (1) One SE aggregate `GROUP BY posting_date, stock_entry_type` (plus item/farm/greenhouse when filtered) for D-6..D, which replaces 64 queries. With `docstatus=1` it uses the composite index.
  - (2) One SLE / Bin query for every stage buffer.
  - (3) One Shelving Log aggregate with `SUM(CASE ...)` for opening, in, out by reason and by hour.
  - (4) One box-label aggregate grouped by flag.
  - (5) Filter options from master tables (Farm, Item in group Roses, Greenhouse) or a cached daily DISTINCT (`frappe.cache`, TTL 1 h). This alone saves about 69 s.
- **Indexes**:
  - `tabShelving Log (shelved_on)`, `(removed_on)`, `(farm, variety)`.
  - `tabBox Label (date)`, `tabOrder Pick List (date_created)`.
  - `tabFarm Pack List (order_pick_list)`, `(creation)`.
  - `tabStock Entry (farm, stock_entry_type, docstatus)` if the greenhouse trace stays.
  - Rewrite `DATE(col)=d` as range predicates.
- **Caching**: the 3-day matrix for closed days is immutable, so cache it per (day, filters). Only today needs live queries.

---

## Page: Bucket Journey (/remote-transfer-v2?tab=journey) — API: `api/remote_transfer/bucket_journey.getBucketShelfOverview`, `getBucketJourney`, `getFarmShelves`, `getBucketsInUse`, `getMissingBuckets`; `api/bucket_replacement`

### What the page shows and how it's computed (brief)
- **KPIs** (`templates/remote_transfer_v2/bucket_journey.html:1033-1046`):
  - Buckets in use = |harvesting ∪ on_shelf ∪ on_truck|. Harvesting = `Bucket QR Code.status='In Use'`; on_shelf = any Shelf Item with a bucket_id; on_truck = pick rows `in_transit=1, shelved=0, modified ≥ today−7`.
  - Empty = total QR − known in-use.
  - Shelves with buckets = shelves having any Shelf Item.
  - Missing = Bucket Replacement status Open.
- **Search**: `getBucketJourney` returns every SE / log / pick row of the bucket. The browser splits these into journeys and computes stems per stage.

### Findings
| ID | Severity | Type | Finding | Evidence (file:line + real-data proof) | Correct definition / fix direction |
|---|---|---|---|---|---|
| MV-20 | High | KPI | **"Harvesting" is always 0 and "Empty" overstates free buckets.** The KPI relies on `Bucket QR Code.status='In Use'`. The agriculture app's nightly `release_stale_buckets` flips every In Use bucket from before today back to Available, so a bucket harvested yesterday and not yet received counts as empty. | `bucket_journey.py:193-195`; `upande_agriculture/scheduled.py:85-108`. Data: all **35,787 QR codes are "Available"**. Overview today: harvesting 0, in_use 3,798, empty 31,989. The 2026-09-24 replay of the flow board showed bucketed harvest still in transit. | In field = buckets whose latest Harvesting SE has no Receiving/Discard SE after it (the same logic as the flow board's `in_transit`, bounded by journey). Don't use the status field for this KPI. |
| MV-21 | Med | KPI/Integrity | **"On shelves" counts any Shelf Item row, including zero-qty rows, and includes the 2,872 stale remote-farm rows** (MV-7). The KPIs therefore show 3,798 buckets in use and 2,198 of 4,053 shelves occupied, with stock dating back to 2026-08-25. | `bucket_journey.py:196-200, 222-233`. 1 Shelf Item has stem_qty ≤ 0. Per farm: Chepsito 1,078 buckets, Kaptumbo 471, Simotwo 600, Torongo 617 (all with no log). | `stem_qty > 0`. Reconcile stale rows (see MV-7). |
| MV-22 | Med | KPI | **"On trucks" is time-boxed by `pli.modified` ≥ 7 days.** Any later edit to the row keeps it alive, and a real truck that has been out for more than 7 days drops off. | `bucket_journey.py:201-209`. No real truck data (0 rows with in_transit=1). Unverified on data. | Use trip state: a bucket on a `Bucket Request Trip` with status Dispatched, not `off_truck`, not shelved. |
| MV-23 | Med | Math | **Client-side harvest/grading stems use "the first entry of that day" for *every* entry of the day.** A bucket with two harvest lines in a day shows 2 × the first qty. | `bucket_journey.html:272-281, 234-238`. Bucket **12FFDT, 2026-09-17**: harvest entries 80 + 120 = 200 stems. The page shows 160 (or 240, depending on which is first). | Server returns `se_qty` per SE. Sum `se_qty[name]` for the journey's SEs and drop the representative-per-day rule. |
| MV-24 | Low | Integrity | `getBucketJourney` includes **draft** Harvesting/Receiving SEs (`docstatus < 2`) in the journey and in the stems. | `bucket_journey.py:46-52`. 47 Chemical Mixing drafts are irrelevant, but drafts for these types are possible. | docstatus=1 for quantities. Show drafts flagged separately. |
| MV-25 | Low | KPI | Missing = `Bucket Replacement` Open. 0 rows on site, so it can't be proven. The definition is consistent with `bucket_replacement.open_replacements`. | `bucket_journey.py:248`, `bucket_replacement.py:150-171`. | OK. |
| MV-26 | Med | Perf | The bunch-mode trace in the browser still chains `frappe.client.get_list` / `get` calls page by page (Shelf docs, OPL docs and FPL docs fetched one by one). | `bucket_journey.html:767-858`. | Add a `getBunchJourney` server endpoint like `getBucketJourney`. |

### Performance profile
- getBucketShelfOverview: **9 queries, 0.87 s wall (0.23 s SQL)**. It loads every in-use bucket id into Python sets (3.8k ids) and then runs `name IN (3,798 ids)`. That is acceptable now but grows with stale rows.
- getBucketJourney: about 9 queries plus `transfer_scheduling.bucket_transfer_trace`. All are indexed by bucket/parent (`custom_bucket_id_index`, `bucket_index`, Shelving Log `bucket_id_index`). `_has()` column checks are cached by Frappe.
- getBucketsInUse "harvesting" part: 2 correlated SED subqueries per bucket (always 0 rows today).

### v2 backend design notes
- Bucket state is one row per bucket from a single `UNION`/`CASE`: latest harvest SE vs latest receive / shelve / issue / discard event per bucket. Better still, maintain `Bucket QR Code.current_stage` on each event (the hooks already exist in stock_movement and mobile). Then all KPIs are one `GROUP BY current_stage`.
- Shelves occupied = `COUNT(DISTINCT parent) WHERE stem_qty>0`. Index `tabShelf Item(parent, stem_qty)`.

---

## Page: Bucket Logistics (/remote-transfer-v2?tab=logistics) — API: `api/remote_transfer/bucket_logistics.getBucketLogistics`, `getBucketLogisticsDetail`

### What the page shows and how it's computed (brief)
- **Server**: per OPL, distinct buckets counted with cumulative "reached" flags: awaiting ⊇ trolley ⊇ transit ⊇ shelved ⊇ ready. It also returns by_farm splits, trips, at_hub, and arrival (first Shelving Log at OPL farm ≥ OPL creation).
- **KPIs** are client-side (`bucket_logistics.html:152-172`):
  - Orders, Σ total, Σ shelved.
  - "at farm" = awaiting − onRoad, where onRoad = transit + trolley.
  - No trip = orders with atFarm>0 and no open trip.
  - Avg transit = mean(arrived − initiated).

### Findings
| ID | Severity | Type | Finding | Evidence (file:line + real-data proof) | Correct definition / fix direction |
|---|---|---|---|---|---|
| MV-27 | High | KPI/Math | **The "at farm" and "on the way" KPIs double count.** The server counts are cumulative (trolley already includes transit, shelved, ready and issued), but the client adds trolley + transit as "on the way" and subtracts both from awaiting. The stage strip also labels the cumulative counts as current ("In trolley", "In transit"). | Server `bucket_logistics.py:41-51`; client `bucket_logistics.html:156,160-169`, `atFarmOf` at 120. **Delivery 2026-09-16**: 3 orders, 15 buckets, all shelved/issued. KPI reads "**0 at farm · 30 on the way**" (30 > 15 buckets that exist). The strip shows In trolley 15, In transit 15, Shelved 15. | Current stage = cumulative differences: at farm = total − reached_trolley; in trolley = trolley − transit; in transit = transit − arrived_hub; arrived (not shelved) = arrived_hub − shelved. Compute these on the server. |
| MV-28 | High | KPI | **"No trip" misses orders.** `atFarmOf = awaiting − trolley − transit` subtracts transit twice. An order with buckets still at the farm and one bucket in transit reads 0 at farm and drops out of "No trip" and "Add to trip". | `bucket_logistics.html:120, 156, 245`. Logic proof: awaiting 2, trolley 1, transit 1 gives 0, but 1 bucket is still at the farm. No live mixed-state data. | at farm = awaiting − trolley (cumulative), or total − reached_trolley. |
| MV-29 | High | Filter | **The farm filter applies to the KPIs but not to the table.** `by_farm`, `at_hub` and `shelved_at` ignore `farm`, and the client renders rows from `by_farm`, so a filtered page lists other farms' groups and counts. | `bucket_logistics.py:274-336` (no `%(farm)s`), `bucket_logistics.html:258-269`. **2026-09-16, farm=Kaptumbo**: KPI Buckets = 1, but OPL-2026-00035 is also rendered under "Chepsito" with 4 buckets. | Add the farm condition to `by_farm`, `at_hub` and `shelved_at`, or filter `by_farm` on the client. |
| MV-30 | Med | KPI | **Issued or ready buckets from a remote warehouse count as transferred and as having passed every stage, with no transfer evidence.** `TRANSFER` includes `issued=1`, and the cumulative flags make issued imply awaiting, trolley, transit and shelved. | `bucket_logistics.py:34-37, 41-45`. **2026-09-16**: 15 buckets with zero awaiting/trolley/transit/shelved flags, 0 trips, `arrived=""`. The page shows "Shelved 15/15 = 100% at hub". | Count stages only from trip events (`Bucket Request Trip Bucket` loaded / arrived / shelved) or from the Remote Transfers SE. Treat issued-without-transfer as "issued at source / unknown route". |
| MV-31 | Med | Math | **Avg transit**: arrival requires a Shelving Log at `o2.farm`, and the log only exists from 2026-09-19. Earlier orders never arrive and are left out of the mean silently. Taking the bucket's *latest* log (MAX creation) can pick a later reuse cycle and inflate the time. | `bucket_logistics.py:358-406`. All orders on 09-05/12/13/16 have `arrived=""` even though they are "shelved". The reuse effect is unverified on data. | Arrival = trip `arrived_at` or the Remote Transfers SE posting time per bucket, bounded to this OPL's trip. Show n (orders averaged). |
| MV-32 | Low | Filter | **The detail and the list disagree on scope.** The detail only drops ready/issued rows at `hub`, and never drops other sales farms. With hub unset (`transfer_hub_farm` empty and 2 sales farms → hub=""), it keeps hub rows too. | `bucket_logistics.py:551-556` vs list `NOT_HUB` 77-79. OPL-2026-00028 with no farm: detail = 3 buckets (2 Kapkolia hub buckets) vs list total 1. The UI always passes farm, which masks it. | Share one `TRANSFER AND NOT_HUB` predicate between list and detail. |
| MV-33 | Low | Integrity | `transfer_hub_farm` is not configured on kaitet (Kapkolia and Karen are both sales shelves), so `hub=""` and HUB labels fall back. | Production Settings singles has no `transfer_hub_farm` value. | Set the setting. Make the page show a config warning. |

### Performance profile
- getBucketLogistics: **14 queries, 0.5–0.66 s**. N+1 spots:
  - `_schedule_map` runs one `get_all` per Packhouse Schedule (14-day window).
  - `_trip_dict(frappe.get_doc(...))` runs per run trip, plus 1–3 queries each.
  - FARM_EXPR (`SUBSTRING_INDEX(...)`) and `UPPER(bucket)` joins can't use indexes. The arrival query joins Shelving Log on `UPPER()` twice.
  - Small today (383 pick rows).
- getBucketLogisticsDetail: 3 queries, fast.

### v2 backend design notes
- Store `source_farm` on Pick List Item (set at allocation, from the warehouse→farm map) and normalise bucket ids to upper case on write. Then joins become indexed equality.
- One query returns per (opl, farm) *current-stage* counts: CASE on the furthest stage reached, `COUNT(DISTINCT bucket)` per stage. The client stops doing arithmetic.
- Stage evidence comes from `Bucket Request Trip Bucket` (loaded / arrived / off_truck / shelved), and arrival from `arrived_at`.

---

### Cross-cutting
- Bucket identity is reused across about 46 harvest cycles per QR code (1.6 M harvests / 35k codes). Every bucket-keyed EXISTS needs a journey bound. `Bucket QR Code.current_journey_start` exists and is indexed, but no movement query uses it.
- `stock_movement.py` puts `custom_bucket_id` on the *line* (SED) for the stage legs, with one SE per variety and length. Queries that read `se.custom_bucket_id` (header) will miss those legs.
