# Stock (ST) — Stock Visibility, Avails, Cold Room

Site kaitet.local, data as of 2026-10-06 (latest shelving 2026-09-24/25, so every "today" figure is currently 0;
"today" behaviour was proven by re-running the same SQL with a fixed past date substituted for CURDATE()).

## Cross-page answer: what is "stems on shelf now"?

| Source | Figure | Verdict |
|---|---|---|
| `Shelf Item.stem_qty > 0` (joined to `Shelf` for location) | **606,241 stems / 3,797 distinct buckets / 2,198 shelves / 3,871 rows** | **Source of truth.** Every allocation, issue and discard path writes it (see `sales_allocation.recompute_bas_quantities` docstring). |
| Stock Visibility "Shelved" / Avails "On shelf" / Cold Room "Stems on shelf" | 606,241 each | **Agree at total level on this site** (no null-variety/null-bucket live rows; every shelf farm is company Karen Roses). They disagree on buckets, farm attribution, age and everything derived (see below). |
| `Shelving Log` (open `Shelved` rows) | 1,003 rows / 131,517 stems | **Not usable.** Logging began 2026-09-19; 2,872 live Shelf Items have no log row; 4 open logs point to deleted/zeroed Shelf Items; 6 open logs have stale qty (net +35). |
| `Bin` / Stock Ledger, the 6 "Receiving Cold Store - KR" warehouses | **150.2 M** stems (e.g. Chepsito 20,353,574 against 201,230 on the shelf) | **Not usable.** `stock_movement.py:1-45` says shelving/allocation/issue never posted to the ledger historically, so stock "piled up in the farm receiving cold stores forever". `_ledger_balance` (stock_movement.py:302) is per bucket only. |
| `Bucket Allocation Status.allocated_quantity` | 33,274 = Σ `Bucket Allocations` not cancelled and not issued (no drift, 0 rows differ) | Correct source for **outstanding allocation**. Cancelled (550) and issued (2,360) rows are already excluded. |
| `Pick List Item` with `issued=0` | 27,270 (submitted OPLs) / 31,879 (including 5 drafts) | Disagrees with BAS by 1,395 to 6,004. 52 outstanding BA rows (5,360 stems) have no matching un-issued PLI by bucket + SO item. The cause is unverified (probably mixed OPL rows keyed differently). |

**True available stems right now = 0.** Every live bucket (606,241 stems) is in one of 10 **Approved, un-actioned**
Discard Requests (DR-2026-003…010, created by `upande_quality/tasks.py:auto_discard_request`). `availability.py`
(the SO autofill and allocation source) therefore returns nothing. **Stock Visibility shows 3,295 available and Avails shows 3,155.**
Both are wrong, and they disagree with each other.

## Page: Stock Visibility (/stock-visibility-v2) — API: api/stock_visibility.getStockVisibilityData

### What the page shows and how it's computed (brief)
The API returns 5 grouped row sets: shelf stock (variety × length × shelf farm, with age bands from
COALESCE(receiving_date, date_added)), allocated (BAS on buckets with any live shelf item, keyed by `shelf_farm`),
discard-requested (DRB rows, `dr.docstatus<2`, `is_shelved=1`, `discarded=0`, bucket still on a shelf), coldroom
(Receiving/Late Receipt in the last 3 days, bucket not on a shelf, not discarded, not issued), and orders
(Σ qty × conversion_factor by variety/length/customer/delivery point/**so.farm**). The page computes the rest client-side
(`www/stock-visibility-v2.html:295-350`): Available = Σ over farm × length of max(0, shelved − alloc − discard).
Variance = Available − Ordered. Cover = Available/Ordered (<1 deficit, <1.1 tight). Age filter = oldest ≥ N.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| ST-1 | Critical | Math | **Discard is netted per DRB row, but a DRB holds only ONE variety/length per bucket.** In a mixed-variety bucket (62 buckets) or a bucket with 2 Shelf Item rows, the other rows look "available" even though the whole bucket is in an approved discard. | stock_visibility.py:83-101. Root cause: `upande_quality/tasks.py:88-121` dedupes `already_listed` per bucket_id, so only the first Shelf Item row is written. Real data: bucket `0217f4` has Mirabel 80 + Azore 20 on the shelf, but DR-2026-006 lists only Mirabel 80. `1b0ce3` has Nikita 30 / Royal Porcellina 10 / Odilia 40, and the DR lists only Nikita 30. In total, 73 live rows / **3,565 stems** sit in DR buckets with no matching DRB variety+length. Page Available = **3,295** (Sweet Sarah Simotwo 52 = 220, Madam Red Chepsito 52 = 200, …). True = **0** (availability.py reserves the whole bucket: 606,241 of 606,241 stems). | A discard reservation is **per bucket**. Available per bucket = 0 if the bucket is in an open DR, else max(0, Σ live stem_qty − BAS.allocated). Aggregate after that. Use exactly one shared definition (availability.py's) for all three pages. |
| ST-2 | High | Math | **Allocated and discard overlap and are subtracted twice.** All 19,584 on-shelf allocated stems (112 BAS) are in buckets that were later placed in approved DRs, so shelved − alloc − disc goes negative (raw −15,839 site-wide). The floor hides this in "Available", but the composition bar ("Where the shelved stems are": available + allocated + in discard) adds up to 625,375 against 606,241 shelved, so its percentages are wrong. | stock_visibility.py:62-101; html:351-356, 388-392. Example: Moonwalk Kapkolia 42cm has shelf 29,800, alloc 10,800 and disc 29,800. Also an operational alert: 19,584 stems allocated to orders sit in buckets queued for discard (auto_discard_request does not skip allocated buckets). | Partition each bucket into exactly one state: discard-reserved, then allocated, then free, so the parts sum to shelved. Flag "allocated but in discard request" as its own alert. |
| ST-3 | High | Math | **Variance double-counts allocated demand.** Ordered = every stem on the SO lines, including stems already allocated to those lines, and Available also subtracts those same allocations. A fully allocated order therefore appears as a deficit equal to its size. | html:341-345, 369-371. Delivery 2026-09-24: Ordered 113,743. 19,064 of those stems are already allocated, 18,834 of them from buckets still on a shelf. The page's variance is understated by 18,834 for that date. | Unmet demand = ordered − (allocated + issued to those SO items). Variance = free available − unmet demand. Allocations to *other* delivery dates still reduce free stock. |
| ST-4 | High | Filter | **The farm filter mixes two meanings of "farm".** Stock is filtered by shelf farm, but orders are filtered by `so.farm` (the selling/packhouse farm: Kapkolia 1,046 lines, Karen 229, NULL 42). Filtering Chepsito gives Ordered = 0 and a false surplus. Filtering Kapkolia gives all 492,865 Kapkolia-sold stems against Kapkolia shelves only. The 42 lines with no farm (14,628 stems) drop out under any farm filter. Avails deliberately treats Ordered as farm-independent, so the two pages disagree. | stock_visibility.py:147; html:328-335. | Demand is not farm-scoped. Apply the farm filter to supply only, or define a sourcing-farm map explicitly. Match Avails. |
| ST-5 | Med | Math | Discard uses `dr.docstatus < 2`, so Draft (and Rejected-but-not-cancelled) requests count, and it ignores `workflow_state`. availability.py uses `workflow_state != 'Rejected'` and ignores docstatus and is_shelved. The definitions differ, which today shows up as 100 stems (bucket DISCARD-TEST-003 is `is_shelved=0`, so the dashboards don't count it but availability.py does). | stock_visibility.py:92-94 vs availability.py:30-41. | One rule: an open reservation = DR with docstatus=1 or a non-Rejected workflow_state, row not discarded. Put it in a shared helper. |
| ST-6 | Med | Integrity | The allocation EXISTS check matches on bucket_id only, not variety+length. A stale BAS on a **reused** bucket gets subtracted from a variety that isn't in that bucket. | stock_visibility.py:71-72; avails.py:49-50. Real data: BAS for `BLK-00001` = Wham 52cm alloc 200 @Kapkolia, but the bucket now holds "Standard Test Variety A" 70cm. 200 Wham 52 stems are subtracted from Kapkolia although none are in that bucket. | Join BAS to the live Shelf Item on (bucket_id, variety, stem_length), as availability.py:66-71 does. |
| ST-7 | Med | KPI | The "Coldroom / awaiting shelving" definition differs from Cold Room's two versions of the same concept. SV uses Receiving + Late Receipt over 3 days. Cold Room "Awaiting shelving (3 days)" uses **Harvesting** entries (the field side, not yet received). Cold Room "Received, not shelved" uses Receiving today only and does not exclude discarded/issued buckets. | As of 2026-09-24 (with the current shelf state): SV **1,400** stems/10 buckets, CR-awaiting **1,592**/14, CR-received-today **1,520**/10. | One definition: latest Receiving/Late Receipt for the bucket's *current journey* (Bucket QR Code.current_journey_start), with no live Shelf Item, no later Discard/issue. |
| ST-8 | Low | KPI | The age basis is COALESCE(receiving_date, date_added), but receiving_date is filled on only 1,045 of 3,871 live rows. Avails uses date_added (TIMESTAMPDIFF, 24h floor) and Cold Room uses the latest Harvesting posting_date. The three "oldest" numbers for the same stock are **42 / 42 / 64 days** (CR's harvest-based oldest is older than the oldest shelving date because of bucket reuse). | stock_visibility.py:39-46; avails.py:27; coldroom.py:147-161. | Store one age anchor on Shelf Item (harvest_date set at shelving from the current journey's harvest) and use it everywhere. |
| ST-9 | Low | Integrity | Ordered = Σ qty × conversion_factor. **Verified correct for mixes:** it equals stock_qty on all 1,317 open lines, including 701 mixed lines (141,123 stems), and equals packrate × boxes except for one straight line with a NULL packrate (SAL-ORD-2026-00098, Radiant Rebecca, 10 stems). | SQL check on open SOIs. | Keep stems = stock_qty (or packrate × boxes). Never sum qty or custom_ordered_quantity (stale: 423,830 vs 525,244 on straight lines). |

### Performance profile
- 5 queries, no N+1. Measured 1.38 s through bench execute (frappe.ping baseline 0.99 s), so about 0.4 s of work.
- Correlated EXISTS on `Shelf Item.bucket_id` (indexed). `Discard Request Bucket` has **no bucket_id index** (3,744 rows; fine today, but grows with every nightly auto-request). The not_shelved query does 3 NOT EXISTS per receiving line. Cheap today because nothing was received in the last 3 days; at the 09-24 volume (998 receivings/day) it still uses the custom_bucket_id index. The comment "bucket ids aren't indexed" is stale (`custom_bucket_id_index` exists).
- The page polls every 60 s.

### v2 backend design notes
- One server-side bucket-level CTE: `live = Shelf Item (stem_qty>0) GROUP BY bucket, variety, length, shelf farm`; LEFT JOIN BAS on (bucket, variety, length); LEFT JOIN open-DR buckets (bucket level). Per row: state = discard | allocated part | free. Aggregate once. Return shelved/alloc/disc/free that **sum exactly**. Run the floor per bucket, never per cell.
- Demand = SOI stock_qty by variety/length for the date, minus the allocated/issued quantity of those same SO items.
- Index: `Discard Request Bucket(bucket_id, discarded)`. Consider a materialised "bucket state" table maintained on shelve/allocate/issue/discard.

## Page: Avails (/avails-v2) — API: api/avails.getAvailsData

### What the page shows and how it's computed (brief)
Shelf (date_added age), allocated (BAS on buckets with a live shelf item), discard (same as SV), orders (ordered +
confirmed stems by variety/length), images, colours. The client pivots variety → farm → length
(`www/avails-v2.html:290-345`): cell avail = max(0, s − a − d). **Variety total avail = max(0, Σs − Σa − Σd)** across all
farms and lengths. KPIs: Ordered (farm-independent), On shelf, Allocated, Available = Σ variety totals.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| ST-10 | Critical | Math | Same as ST-1 (per-row discard, which leaks the second rows of mixed buckets) and ST-2 (allocated+discard overlap). Available KPI = **3,155** against a true 0. | avails.py:59-77; html:322-339. | Bucket-level state as in the SV notes. |
| ST-11 | High | Math | **Floors at different levels give different answers.** The variety total and the length column total net an over-committed farm/length cell against a positive one elsewhere. The farm cells (and Stock Visibility) floor per cell. So the variety row disagrees with the sum of its own farm rows, and Avails disagrees with Stock Visibility. | html:330-343 (by.avail per length, totAvail per variety). Real data: **Fireworks** shows 130 on SV and **10** on Avails. Bellalinda Cerise shows 20 on SV and 0 on Avails. Site-wide 3,295 (SV) vs 3,155 (Avails). | Over-commitment in one bucket/farm cannot be met from another implicitly. Floor per bucket (ST-1), then sum. Show over-commitment as a separate alert. |
| ST-12 | High | Filter | **The age filter keeps whole groups.** "On shelf ≥ N days" filters the aggregated (variety, length, farm) row by its *oldest* bucket (MIN date_added), so every fresh stem in that group passes. Allocations and discards are not age-filtered, so Available becomes shelf(aged) − all alloc − all discard (the page admits the over-commit hint is "only meaningful without the age filter", but the KPI is still shown). | avails.py:27, html:309. Real data, as of 2026-09-24 with "≥ 7 days": Fireworks 62cm Karen has oldest 7 d, so all 1,030 stems pass, although 680 were shelved 09-20 to 09-24 (≤ 4 d). | Bucket-level age, filtered before aggregation (return age bands or per-bucket rows). Apply the same bucket filter to its alloc/discard. |
| ST-13 | Med | Integrity | Allocated EXISTS matches on bucket only (ST-6). Allocations are not double-counted across OPL rows or cancellations: BAS.allocated_quantity = Σ outstanding Bucket Allocations exactly (0 drift; 550 cancelled and 2,360 issued correctly excluded). One BAS row per (bucket, variety, length), so there is no fan-out. **Verified OK.** | SQL: BAS 33,274 = BA outstanding 33,274. | Keep BAS as the allocation source. Fix the join (ST-6). |
| ST-14 | Low | KPI | Ordered is correct for mixed lines (ST-9). `confirmed_stems` is computed but never used by the v2 page. Filter options come only from shelf+allocated, so ordered-only varieties can't be selected (although the Ordered KPI counts them). | avails.py:85-93; html:240-249, 360-368. | Drop the unused column or show it. Build the options from the union including orders. |

### Performance profile
- 4 SQL queries + 1 Item get_all + 1 File get_all + 1 colour query, so 6-7 queries with no N+1. Measured 1.74 s (≈0.75 s of work above bench overhead). The File lookup by `attached_to_name IN` is cheap at this size.

### v2 backend design notes
- Reuse the same bucket-state CTE as Stock Visibility (one shared function). Avails and SV should be two views of one dataset.
- Cache the images/colours (they change rarely) separately from the stock numbers.

## Page: Cold Room (/cold-room-v2) — API: api/coldroom.fetchColdroomData, getColdroomBuckets

### What the page shows and how it's computed (brief)
fetchColdroomData: shelf stock grouped by variety/length/shelf farm (company hard-coded 'Karen Roses'), Harvesting-based
"not shelved", a second full shelf scan for bucket ids, harvest-age per bucket (literal IN list), Receiving-today
incoming, OPL PLI not issued **created today**, Discard SEs today, Cold Store Capacity, Production Settings, farms. The
server totals stems/buckets/varieties/shelves/age; the page draws KPIs, a pressure bar per cold store, a flow list and charts.
getColdroomBuckets: shelf rows, harvest age, today's discard-request buckets still shelved, received today not shelved,
today's OPL rows not issued.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| ST-15 | High | KPI | **"Allocated, not issued" counts only OPLs created today** (`date_created = CURDATE()`), submitted only. Outstanding allocations from earlier days disappear, and drafts are ignored. | coldroom.py:247-263, 538-551. Today it shows **0**, while BAS shows 33,274 outstanding (27,270 on submitted OPLs + 4,609 on drafts). On 2026-09-24 it would show 18,890 against 27,270 outstanding. | Outstanding = BAS.allocated_quantity on buckets in this cold room (or PLI issued=0 on OPL docstatus<2, regardless of date). |
| ST-16 | High | KPI | **The bucket list's "discard but still shelved" is limited to requests created today** (`dr.creation >= CURDATE()`). Approved but un-actioned older requests vanish, and the empty state says "Every bucket on today's discard list has left the shelves". | coldroom.py:486-503. Today: **0** listed, while **3,796 buckets / ~602k stems** are in approved un-actioned DRs (DR-2026-003…010, created 09-25 and 09-30). | Open DR rows (docstatus=1, not discarded) whose bucket still has a live Shelf Item, any date. Sort by age. |
| ST-17 | Med | Math | **Bucket count overcounts mixed buckets.** total_buckets = Σ COUNT(DISTINCT bucket) per (variety, length, farm) group. The farm_buckets doughnut and per-row bucket counts behave the same way. | coldroom.py:39, 344-363. Real: KPI **3,869** vs **3,797** distinct buckets (+72). | COUNT(DISTINCT bucket_id) over the whole filter scope, computed in SQL. |
| ST-18 | Med | Math | **The age histogram loses stems.** `shelf_bucket_stems[bid] = stem_qty` overwrites per row, so a mixed bucket contributes only its last row. Buckets with no Harvesting entry are dropped. Average age is an unweighted mean per bucket, not per stem. | coldroom.py:125-131, 163-181. Real: the chart covers 606,241 − (2,305…4,730 lost from multi-row buckets) − 600 (3 buckets with no harvest). | Σ stems per bucket. Use one age anchor on Shelf Item (ST-8). Report a stem-weighted average and the oldest. |
| ST-19 | Med | Math | **Age uses the latest Harvesting of a reused bucket id.** 494 live Shelf Items (81,710 stems) belong to buckets that have a Harvesting entry dated *after* the item was shelved, so their age resets and looks too young. The opposite also happens: oldest = 64 d by harvest vs 42 d by shelving date. The same lookup drives `auto_discard_request` (tasks.py:73-80, with no docstatus filter). | coldroom.py:147-161. Example: bucket VV2PD9 (Aqua, CHP-D2B) was shelved 2026-09-01, and Harvesting was posted 2026-09-04. | Age comes from the current journey (Bucket QR Code.current_journey_start) or from Shelf Item.harvest_date. Never MAX over all history. |
| ST-20 | Med | KPI | The pressure bar (v2 page) passes the **site-wide** allocated total to every farm's cold store (`store(c.farm, …, allocated, …)`), so each store's "allocated" segment = min(site alloc, farm shelf), and the per-store allocated values add up to more than the site total. Latent here because `tabCold Store Capacity` **does not exist** on kaitet (the query fails silently and the page falls back to one "All cold stores" bar). `tabProduction Settings` also does not exist (it is a Single doctype), so settings are always the defaults. | cold-room-v2.html renderPressure/store. coldroom.py:297-322. | Per-farm allocated (BAS by shelf_farm). Read the Single via frappe.db.get_single_value. Check that the table exists instead of using try/except. |
| ST-21 | Med | Filter | **Company is hard-coded to 'Karen Roses'** in 6 queries. Kaitet Ltd. farms (Endebess, Lokitela, Saboti, Vale) are invisible on Cold Room but visible on SV/Avails (SV removed the hard-code for this reason, stock_visibility.py:105-107). Totals agree today only because all shelved stock is at KR farms. | coldroom.py:46, 75, 116, 217, 282, 328, 427, 514. | Filter by the user's/selected company or by farm list. Never a literal. |
| ST-22 | Low | Filter | The farm filter applies to different farms per metric: shelf by `Shelf.farm`, incoming/not-shelved by `se.farm`, allocated by `opl.farm` (the packhouse farm), and the bucket-tab discards by `dr.farm`. 9 live rows (1,050 stems) have `Shelf Item.farm ≠ Shelf.farm` (e.g. DEMO01-03 on Chepsito shelf KPK01B with si.farm NULL). availability.py uses si.farm, while the dashboards use Shelf.farm. | coldroom.py:18-29, 412-416. | The physical location is Shelf.farm everywhere. Fix or drop Shelf Item.farm. |
| ST-23 | Low | Integrity | Received-today/incoming is excluded if the bucket id is on *any* shelf, including a stale old Shelf Item of a reused bucket, and it does not exclude discarded/issued buckets. | coldroom.py:206-244, 506-535. | Key on journey (current_journey_start), not bare bucket id. |

### Performance profile
- fetchColdroomData: 10 queries (2 fail silently on missing tables), and the shelf join is scanned twice (stock_data and shelf_buckets). getColdroomBuckets: 5 queries.
- **Measured: fetchColdroomData 12.4 s, getColdroomBuckets 11.5 s** (bench baseline ≈1 s). Nearly all of it is the harvest-age query: `Stock Entry WHERE custom_bucket_id IN (<3,797 literal ids>) AND type='Harvesting' GROUP BY bucket`, which takes **≈10 s alone**. It uses `custom_bucket_id_index` and then does random row lookups on a 3.36 M-row table (86,327 matching entries, 79,838 Harvesting) because type/docstatus/posting_date aren't in the index. Both endpoints run it, and the page polls every 120 s (both endpoints when the Buckets tab is open), so one open tab costs about 22 s of DB time every 2 min.
- The literal IN list is built with string concatenation using quote doubling only (coldroom.py:142-145, 444-447). The values come from the DB, but a backslash in a bucket id would break the query (low risk).

### v2 backend design notes
- Drop the Stock Entry age lookup entirely. Age comes from Shelf Item (harvest_date/date_added, set at shelving from the current journey). If a ledger lookup is ever needed, add the composite index `Stock Entry(custom_bucket_id, stock_entry_type, docstatus, posting_date)`.
- One shelf query → bucket-level rows → totals (distinct buckets, distinct shelves, stem-weighted age) computed in SQL. Reuse the bucket-state CTE from SV/Avails for allocated/discard so the Cold Room's "allocated" equals Avails' "Allocated".
- "Awaiting shelving" has one definition (ST-7), shared with SV.
- Cache 30-60 s per farm. Shelf state changes are event-driven, so a short cache with explicit invalidation on shelve/allocate/issue/discard is safe.

## Summary of agreement across pages (current data)
| Metric | Stock Visibility | Avails | Cold Room | Truth |
|---|---|---|---|---|
| Stems on shelf | 606,241 | 606,241 | 606,241 | 606,241 (Shelf Item) |
| Buckets | — | — | 3,869 | 3,797 |
| Allocated (outstanding) | 19,584 (on-shelf) | 19,584 | 0 (today's OPLs only) | 33,274 (19,584 on shelf) |
| Discard-flagged | 602,496 | 602,496 | 0 listed (today's DRs only) | 606,241 (all buckets reserved) |
| Available | 3,295 | 3,155 | n/a | **0** |
| Oldest age (days) | 42 | 42 | 64 | 42 by shelving date (age anchor undefined, ST-8) |
