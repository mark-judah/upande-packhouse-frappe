# WF — Workflow, Production, Discards, Downgrades

Site: kaitet.local. Every number below comes from read-only SQL, or from running the API function in a `bench console` with `frappe.local.form_dict` set and a rollback afterwards. I read each function end to end first and none of them writes. Scripts: scratchpad/wf/*.py. "Sept" means the range 2026-09-01..2026-09-30.

---

## Page: Workflow (/packhouse-dashboard-v2) — API: api/dashboard.py `getDashboardData`, `getTeams`, `_boxes_to_deliver`

### What the page shows and how it's computed (brief)
The page sends `period=custom, from_date/to_date, team_filter, rose_type, boxes_from/boxes_to` (the same dates).
- **OPL population.** `Order Pick List` filtered by `date_created BETWEEN from AND to`, plus `team IN (...)`. No docstatus filter (dashboard.py:59-64).
- **Order pick lists KPI** = `total_opls` = the number of OPLs that remain after the rose filter.
- **Ready to issue** = the count of OPLs with `docstatus == 1` (dashboard.py:519-521). The tiles show the same list.
- **Boxes packed** = `total_box_progress` = Σ packed / Σ expected.
  - *Expected* = Sales Order Items where `custom_opl IN opls`, deduped per OPL by `custom_line` → `bunch_group` → `mix_group` (dashboard.py:214-273).
  - *Packed* = the number of distinct `box_id` per FPL, summed over FPLs with docstatus != 2 (dashboard.py:278-299).
- **Issuing %** = issued stems ÷ pick stems (`Pick List Item.stock_qty`, `issued=1`).
- **Packing %** = FPL `stock_qty` ÷ `OPL.custom_total_stems`.
- **Fulfilment %** = total_pkd ÷ total_exp.
- The **gap badge** is worked out on the client as `global_packing_pct − global_issuing_pct`.
- **Stems to deliver** (`_boxes_to_deliver`, dashboard.py:626-694):
  - Sales Orders with `delivery_date` in range and docstatus 1.
  - Stems = Σ SOI `stock_qty`. Boxes are deduped per SO by `(custom_line)` / `bunch` / `mix` / line.
  - The team filter is applied through OPL.sales_order.
- **Per OPL:**
  - `total_stems` = `OPL.custom_total_stems`.
  - `total_bunches` = `total_stems // 10`.
  - `box_progress` = packed/expected as above.
  - `rose_type` comes from the item groups of the pick-list items.

### Findings
| ID | Severity | Type | Finding | Evidence (file:line + real-data proof) | Correct definition / fix direction |
|---|---|---|---|---|---|
| WF-1 | Critical | Integrity/Math | **Expected boxes are inflated because `Sales Order Item.custom_opl` is copied into duplicated orders.** The field has `no_copy=0`, so when an SO is duplicated, its lines keep the old OPL link. The dashboard sums `custom_number_of_boxes` of every SOI whose `custom_opl` is the OPL, including lines from other orders that this OPL never picked. | dashboard.py:216-228. Custom Field `Sales Order Item-custom_opl` has no_copy=0. 56 of 124 linked SOI rows point at an OPL of a *different* SO, and 57 are not in the OPL's pick list. 45 of the 49 Sept OPLs are affected. **OPL-2026-00017:** shown `3/11`. Its own line is 3 boxes (Packing Guide also 3). The extra 8 boxes / 6,720 stems come from line `7veu73pqga` of SAL-ORD-2026-00126. **Sept total expected:** shown **267**; Packing-Guide boxes **135**. | The planned boxes of an OPL = `COUNT(DISTINCT box_number)` from `Packing Guide` (table_nade). Fall back to deduped SOI only for OPLs with no guide, and only for SOIs actually referenced by `Pick List Item.sales_order_item` of that OPL. Also: set `custom_opl` no_copy=1 and clean up the stale links (cross-area). |
| WF-2 | High | Math/KPI | **Fulfilment % and "Boxes packed x/y" use the inflated denominator from WF-1.** | Sept: shown 36/267 = **13%**. Against Packing Guide boxes the correct figure is 36/135 ≈ **27%**. The 36 is itself a rough count (see WF-5). | Fulfilment = completed boxes (Box Label / complete FPL boxes) ÷ Packing Guide boxes. |
| WF-3 | High | Math | **Per-OPL "Stems" and the packing % denominator are allocated stems, not planned stems.** `custom_total_stems` = Σ pick-list `stock_qty` (sales_allocation.py:2137-2152, 2455-2456). It is also updated by raw SQL that can go stale. | **OPL-2026-00048:** custom_total_stems 600 vs pick list 580. **OPL-2026-00027:** 200 allocated vs Packing Guide 500. **OPL-2026-00022:** 320 vs 370. With an allocated-only denominator, packing hits 100% (capped at :443) before the box plan is met. | Planned stems = `SUM(Packing Guide.stems)`. Show allocated stems separately (Σ Pick List Item.stock_qty, computed live rather than read from `custom_total_stems`). |
| WF-4 | High | Math | **`total_bunches` = stems // 10 is hard-coded.** | dashboard.py:428. SOIs use Bunch (1, 2, 3, 4, 5, 7, 9, 13, 15, 20, 25) and Stems — 260 lines in all. **OPL-2026-00017:** shown 198 bunches; real 132 (Bunch (15); PLI qty 132, Packing Guide bunches 132). **OPL-2026-00036:** shown 12; real 24 (Bunch (5)). **OPL-2026-00055:** shown 338; real 376 (Bunch (9)). | Bunches = `SUM(Pick List Item.qty)` (stock_qty ÷ conversion_factor), or `SUM(Packing Guide.bunches)` once that is reliable. Note: Packing Guide bunches look wrong on OPL-00022 (82 for 320 stems) and OPL-00026/27 (500) — flagged for the packing area. |
| WF-5 | Med | Math | **"Packed boxes" counts any box_id that has at least one FPL row, including boxes still being packed in draft FPLs.** It is not checked against the guide's box numbers. Separately, labels that exist without a matching FPL row are invisible here (e.g. 6 TEST-* labels on OPL-2026-00043, 1 label each on draft OPLs 00030-00033). | dashboard.py:279-299. FPL docstatus != 2 includes drafts. | Packed box = a box_id where every (box, variety) in the guide is met or the box has an `under_pack_reason`. Equivalently, one Box Label per box (non-test). Count it once per OPL (box_id is per OPL), not per FPL. |
| WF-6 | High | KPI | **"Ready to issue" = every submitted OPL, even ones that are already fully issued or packed.** | dashboard.py:503, 519-521. In Sept, 44 OPLs are "ready". Among them, OPL-00004/24/25/34/37/43/44/46 have issuing 100% and packing 100%. | Ready to issue = docstatus 1 AND at least one Pick List Item with `issued=0`. Optionally also not fully packed. |
| WF-7 | High | Filter/KPI | **No docstatus filter on OPLs.** Draft OPLs (docstatus 0) count toward "Order pick lists" and every aggregate (pick stems, planned, expected boxes, averages). Cancelled OPLs (docstatus 2) would be counted too, under the label "Partially Allocated" (:499). | dashboard.py:59-64. Sept: total_opls = 49, ready = 44. The 5 drafts (OPL-00013..16, 26) add 0/24 boxes and 4,569 stems to the denominators. | Exclude docstatus 2 always. Decide whether drafts belong in the KPIs; if they stay, label them separately. |
| WF-8 | High | Filter | **The date range applies to `OPL.date_created`, but "Stems to deliver" applies it to `Sales Order.delivery_date`.** The KPI row therefore mixes two different populations. On top of that, OPLs with `date_created` NULL never appear for any range. | dashboard.py:44 vs :632. There are 6 OPLs with NULL date_created (00029-00033 drafts; **00042 submitted**). On 2026-09-24 the page shows 9 OPLs but 65 orders / 306 boxes "to deliver". | Use one date semantic per page. Recommended: the dispatch/delivery date of the SO behind the OPL (`OPL.sales_order → SO.delivery_date`, or the item delivery_date), with `date_created` as a fallback. Backfill date_created from creation. |
| WF-9 | Med | Filter | **The rose-type filter keeps or drops whole OPLs.** A mixed OPL stays in under both "spray" and "standard" with its full stems. The KPIs (pick, issued, planned, packed) are not split by type. "Stems to deliver" ignores the rose filter completely. | dashboard.py:194-209 and :597-601 (no rose arg). Unverified on real data: no Sept OPL is "Mixed". The logic is certain. | Split stems per type with the item-group tree (as the code already does for spray/std). Apply the filter to the SO lines in `_boxes_to_deliver` as well. |
| WF-10 | Med | Filter | **Team filter edge cases.** (a) OPLs with no team (9 drafts) cannot be selected; "Unassigned" is not an option. (b) Stems to deliver for a team counts the *whole* SO when any OPL of that SO belongs to the team. Lines allocated to other teams are included, and SOs with no OPL yet are dropped. | dashboard.py:53-56, 642-650. | Map team at line level (SOI → OPL via the Pick List Item.sales_order_item, rather than custom_opl). Add an "Unassigned" option. |
| WF-11 | Low | Math | **Box dedup in `_boxes_to_deliver` and in the expected-boxes code differs from the canonical `_set_order_summary`.** (a) The spec key here is `custom_line` only; the canonical key is `(custom_line, custom_length)`. (b) Bunch/mix keys here require the `custom_mixed_*` flag; the canonical code does not. (c) Expected-boxes keys use OPL+group, so when contaminated lines from two SOs share mix_group "1" or the same spec, they collapse. | dashboard.py:255-260, 680-690 vs sales_order_engine.py:139-145. No current SO has one spec at two lengths, so the Sept result is identical: **2,167** boxes both ways. | Share one `order_box_key(line)` helper with `_set_order_summary`. |
| WF-12 | Med | Integrity | **The stored `Sales Order.custom_total_boxes` is stale on 74 of 368 Sept SOs**, saved before the dedup fix. Any page that sums it overcounts. Separately, SOI `stock_qty` disagrees with packrate × boxes on 1 line. | Σ stored custom_total_boxes for Sept = **2,658** vs canonical recompute **2,167**. Examples: SAL-ORD-2026-00175 stored 120, correct 24 (five 24-box lines in one mix group / spec); SAL-ORD-2026-00221 stored 4, correct 1. Stems: Σ stock_qty 666,067 vs canonical 666,057 (SOI `4lhning0ot` on SAL-ORD-2026-00098: stock_qty 10, packrate NULL). | `_boxes_to_deliver` computes boxes correctly today. Re-save or backfill `custom_total_boxes`/`custom_total_stems` so v2 can read the stored values. |
| WF-13 | Med | KPI | **Issuing % reads 0% on OPLs that are already 100% packed.** The `issued` flag is set only by mobile flows (mobile/api.py:4539, 4683), so OPLs packed without the issue scan stay at 0%. The "Packing ahead by X" badge then reads as a real gap. | OPL-2026-00017, 00047 and 00050: issued 0 of 22/6/3 rows, packing 100%. Sept: issued 7,244 vs packed 10,400 stems. Across the site only 80 of 383 PLI rows are issued. | Treat a bucket as issued if it has an `issued=1` row OR its stems reached an FPL / issue Stock Entry (`custom_opl_scanned`). Otherwise show the gap as "unscanned". |
| WF-14 | Low | Bug | **Schedule ordering is dead code.** It reads `custom_schedule_number`, which is never fetched; the real field is `schedule_number`. The bubble sort never runs, and tiles always return `custom_schedule_number: ""`. | dashboard.py:62, 74, 505. The OPL JSON has `schedule_number`. | Fetch `schedule_number` and use `ORDER BY` in SQL. |
| WF-15 | Low | Filter | **The `period=today`/`yesterday` paths filter Box Label `creation = 'YYYY-MM-DD'`** — a datetime compared to midnight. The v2 page always sends `custom`, which uses `between` and is handled correctly, so only legacy callers are affected. `boxes_printed_today` / `box_labels_today` are returned but not shown by v2. | dashboard.py:381-400. | Remove these fields, or use a `[d, d+1)` range. |
| WF-16 | Low | KPI | **Averages are plain means.** `avg_issuing_pct` / `avg_packing_pct` count OPLs with no pick rows as 0%. They are returned but not used by v2; v2 uses the stem-weighted globals, which is correct. | dashboard.py:540-541. | Drop them, or weight by stems. |

### Performance profile
- **Queries per request:** about 9 fixed. OPL, PLI, Item, Item Group ×2, SOI, FPL, FPL Item, Bypass Log, under-pack join, Box Label, then SO, OPL (team), SOI for deliver. No per-row DB calls.
- **Measured** (console, includes 30-day boxes_to_deliver): Sept range **0.72 s**; one day 0.016 s.
- **Missing indexes:**
  - `tabOrder Pick List` has none on `date_created`, `team` or `sales_order`.
  - `tabSales Order Item.custom_opl` is unindexed, so `custom_opl IN (...)` scans the whole table (1,373 rows now; grows linearly).
  - `tabFarm Pack List.order_pick_list`, `tabPacking Bypass Log.order_pick_list` and `tabSales Order.delivery_date` are unindexed.
- **Quadratic Python:**
  - List concatenation `x = x + [..]` inside loops (opl_names, processed_opls, ready_to_issue, issues).
  - `seen = seen | {key}` copies the whole set on every SOI (dashboard.py:266, 692).
  - Bubble sorts at :92-98 and :463-473.
  - Fine at 55 OPLs; this becomes O(n²) with thousands of OPLs or SOIs.
- **Response size:** ready_to_issue duplicates opls, so every OPL row is sent twice.

### v2 backend design notes
- **Population:**
  - OPLs with docstatus < 2 (or = 1) and a date in range.
  - Choose one date: recommended `SO.delivery_date` via `OPL.sales_order`, falling back to `DATE(OPL.creation)`.
  - Index `(date_created)`, `(team)`, `(sales_order)` on OPL.
- **Per-OPL aggregates in one grouped query each, keyed by OPL:**
  - Allocated/issued stems and bunches: `SELECT parent, SUM(stock_qty), SUM(qty), SUM(issued*stock_qty) FROM tabPick List Item WHERE parent IN … GROUP BY parent`.
  - Planned boxes/stems: `SELECT parent, COUNT(DISTINCT box_number), SUM(stems) FROM tabPacking Guide … GROUP BY parent`.
  - Packed stems and complete boxes: FPL items joined with the guide per (box, variety), with under_pack_reason. Or count Box Labels (exclude TEST-* / docstatus 2).
  - Rose type: PLI ⨝ Item ⨝ Item Group (lft/rgt), grouped into spray/standard stems per OPL. The filter then becomes `spray_stems > 0` and the KPIs use the per-type stems.
- **Stop using `SOI.custom_opl`** for anything until it is set no_copy and backfilled.
- **Stems to deliver:**
  - Σ line stems (packrate × boxes, or stock_qty once it is validated).
  - Boxes via the shared canonical key helper (or stored `custom_total_boxes` after backfill).
  - Apply team and rose filters at line level.
- **Caching:** the page polls every 30 s per user, so cache the payload ~15 s per (range, team, rose) key. Bust it on OPL/FPL/Box Label submit.

---

## Page: Production (/packhouse-production-v2) — API: api/production.py `get_packhouse_production_by_variety`, `getProductionLocations`

### What the page shows and how it's computed
- **Query.** One SQL over `Stock Entry` (type Harvesting, docstatus 1, posting_date in range) ⨝ `Stock Entry Detail` ⨝ Item. It sums `sed.qty`, grouped by item, `se.custom_stem_length` and `se.farm`.
- **Rose filter.** `i.item_group = 'Standard Roses' | 'Spray Roses'`.
- **Location filter.** `farm IN (Farm where farm_location = X)`.
- **Client side.** The page derives the length mix, the farm list, the top variety and the main length from the variety rows.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| WF-17 | Critical | Filter | **The rose-type filter compares item_group with a flat string.** Real items sit in sub-groups ("Spray Roses - The Classics", "Standard Roses - Intermediate"…), so Standards return almost nothing and Sprays return only the few items still in the root group. | production.py:21-27. Harvest 2026-09-18..24: **Spray shown 7,140** stems (2 varieties) vs true **89,545**. **Standard shown 0** vs true **83,249**. | Use `item_groups.resolve_rose_item_groups`, or `JOIN tabItem Group g ... AND g.lft BETWEEN root.lft AND root.rgt`. |
| WF-18 | Critical | Perf | **With the rose filter on, the query takes 57-83 s.** MariaDB starts from `Item` by item_group, then reads every historical SED row for those items through `item_code_t_warehouse_index` (3.36 M SED rows), and only then checks the date. | Measured: spray 7 days **57.4 s and 83.0 s**; no filter 7 days 0.66 s; 30 days 9.7 s. The EXPLAIN shows `i` → `sed` (ref item_code) → `se` (eq_ref PK). | Drive from `se` (index `stock_entry_type_docstatus_posting_date_index` already exists, e.g. STRAIGHT_JOIN), and filter the item group with `IN (SELECT name FROM Item WHERE item_group IN (subtree))`. Better still, keep a daily harvest summary table. |
| WF-19 | Med | Math | **"Stems harvested" includes non-rose items.** In 2026-09-18..24 that was 3,500 Lepidium stems plus 200 stems of an item with no group. | Same week: total 176,494, of which 3,700 are not roses. | Default "All roses" to the union of both rose subtrees, or label the KPI "all items". |
| WF-20 | Med | Filter | **The location filter only knows farms with `farm_location` set.** 11 of 16 farms (Eldama, Endebess, Kaptumbo, Saboti, Westwood…) have NULL and can never be selected. Harvests from them only appear under "all", and 10 harvest SEs with no farm show as "Unknown". | `SELECT name, farm_location FROM tabFarm` → only Chepsito/Kapkolia/Simotwo/Torongo → Kapkolia, and Karen → Karen. | Backfill Farm.farm_location; add an "Unassigned" option. |
| WF-21 | Low | Math | **Stem length is read from the SE header (`se.custom_stem_length`).** A harvest SE whose rows are at different lengths would be assigned a single length. Qty is `sed.qty`; in this data it equals transfer_qty and every row is UOM Stems. Use transfer_qty to be safe. | production.py:45, 47. | Use the row-level length if one exists, and `sed.transfer_qty`. |

### Performance profile
- **Queries:** one main query plus one for locations. 7 days takes 0.66 s; 30 days takes 9.7 s, because each harvest SE has about 1 row and there are about 5k per day. The rose filter path takes 57-83 s.
- **Indexes:** the composite (stock_entry_type, docstatus, posting_date) exists. Every request still scans all harvest SEs in the range and joins SED by `parent`.
- **Polling:** the page polls every 30 s, which re-runs a ~10 s query for a 30-day range.

### v2 backend design notes
- **Source of truth:** submitted Harvesting SEs. Rose type comes from the Item Group subtree (lft/rgt). Location comes from `Farm.farm_location`, with "Unassigned" for NULL.
- **Pre-aggregate:** keep a `Harvest Daily Summary(date, farm, item_code, stem_length, rose_type, stems)` table. Fill it on SE submit/cancel, or with a nightly job plus incremental updates for today. The page then reads at most a few thousand rows, in well under 50 ms.
- **Until then:** force the driving table with STRAIGHT_JOIN from `se`, and cache 60 s per key.

---

## Page: Discards (/packhouse-discards-v2) — API: api/discards.py `getDiscardData`

### What the page shows and how it's computed
- **Population.** `Discard Request Bucket` ⨝ `Discard Request` where workflow_state = 'Approved' and `COALESCE(approval_date, requested_date)` is in range.
- **KPIs.**
  - Buckets = COUNT.
  - Stems = SUM(stem_qty).
  - Avg age = AVG(age_days).
  - Foregone = stems × the selling rate from the valuation price list (Production Settings, default "EUR Price List") at (variety, stem_length), shown with a KES equivalent at today's rate.
- **Rows.** 1,000 rows at most, each with its own rate looked up in Python.
- **Rose filter.** `dr.item_group = 'Standard Roses' | 'Spray Roses'`.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| WF-22 | Critical | Filter | **The rose filter can never match.** The `Discard Request.item_group` header is NULL on all 10 requests, and the comparison is a flat string anyway. Spray or Standard always shows 0. The table's "Type" column (`rose_group` = dr.item_group) is always "—". | discards.py:23-28, 42. Sept spray: shown **0 buckets / 0 stems**. True (bucket variety → Item → Spray subtree): **1,097 buckets / 87,958 stems**. | Classify per bucket: `drb.variety → Item.item_group → subtree`. |
| WF-23 | High | Math | **Total foregone value is over-counted when a variety/length has more than one live Item Price.** The SQL join multiplies the bucket's stems by every matching price row, while the per-row values pick a single price. KPI and table therefore disagree. | discards.py:136-148 vs :117-132. On 2026-09-30, `total_foregone` = **17,167.30 EUR**, but the sum of row foregone = **16,934.10 EUR** (all 989 rows returned, not truncated). The difference of 233.20 = 530 stems of "Good mood 62cm" at €0.44, which has two price rows (valid 2026-09-04→2030-02-04 and 2026-09-04→open). | Pick one price per (item, length) — the latest valid_from, valid on the discard date — in a CTE/window function, then join once. |
| WF-24 | High | KPI | **Most stems are unpriced and the page does not say so.** Only 2,126 of 3,799 approved buckets have any EUR Price List price. The rest add 0 to the foregone KPI, and the only sign is a "—" on each row. | COVER query: 3,799 buckets, 2,126 priced. Sept rows: 453 of 1,000 have no rate. | Return `unpriced_buckets` / `unpriced_stems` and show them in the KPI. Consider falling back to the price list's own default length price. |
| WF-25 | Med | Math | **Foregone value uses today's price and today's FX rate,** not the ones in force on the discard date. Price rows with a NULL `valid_from` are dropped by the SQL total but accepted by the Python row logic (none exist today, so this is latent). The price lists also mix UOM `Nos` and `Stems`. | discards.py:123, 142, 155. | Value at the discard date: `valid_from <= d AND (valid_upto IS NULL OR valid_upto >= d)`, and the exchange rate on date ≤ d. Normalize per stem by UOM. |
| WF-26 | Low | KPI | **"Avg age" is a per-bucket average, not stem-weighted.** "Location" (`dr.coldroom`) is NULL on every request, so that column is always blank. 2 approved requests have 0 stems and a NULL variety but still count as buckets. | discards.py:52; data: DR-2026-001/002. | Weight by stems; drop empty buckets; get location from the bucket's shelf. |

### Performance profile
- **Queries:** 5 aggregates over the same join (rows, agg, by_variety, by_farm, total foregone), plus a price lookup and an FX lookup. About 7 queries, all sequential.
- **Measured:** Sept 0.88 s; one day 0.32 s.
- **Missing indexes:** none on `Discard Request.workflow_state`, `approval_date` or `requested_date`. `Item Price` has no composite index on (price_list, item_code, custom_length).

### v2 backend design notes
- **Single pass:** one CTE over the bucket rows in range, with a priced rate per bucket and a rose type per bucket, then GROUP BY ROLLUP (or compute in Python from the CTE result, which has fewer than 4k rows per month).
- **Dates:** index `(workflow_state, approval_date)` and backfill approval_date, so the date is plain rather than COALESCE.
- **Rate table:** precompute one row per (price_list, item, length, valid range) and join on the discard date.

---

## Page: Downgrades (/packhouse-downgrades-v2) — API: api/downgrades.py `getDowngradeData`

### What the page shows and how it's computed
- **Population.** Pick List Items with `downgrade_reason` set, on OPLs with `date_created` in range, optionally filtered by team.
- **Original length.** A correlated subquery takes the latest Harvesting SE for the bucket (by creation). Rows where pick length equals original length are dropped.
- **Pricing.** The sold rate is SOI rate ÷ conversion. The original-length rate comes from a price list chosen by **SO currency** (Production Settings "Currency Price List", otherwise "<CUR> Price List").
- **Foregone** = (orig − sold) × line stems, summed per currency and converted to KES at today's rate.
- **Filters.** Reason and owner filters are applied in Python after the 2,000-row cap. "Available stems" = Σ `available_stems_of_exact_length`.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| WF-27 | Critical | Math | **Foregone value is never computed.** The SO currency is joined through `pli.sales_order`, which is empty on all 383 Pick List Items. So `so_currency` is NULL, no price list is chosen, `foregone` is None, `foregone_by_currency` is {}, and `unmapped_currencies` is [] — the page shows "nothing priced in range" with no warning. | downgrades.py:78, 168-173, 247-248. `SUM(IFNULL(sales_order,'')='')` = 383/383. Sept: 2 lines, `foregone_by_currency {}`, KES 0. The "Currency Price List" table is also empty. | Join `tabSales Order so2 ON so2.name = soi.parent`. Warn when a line has no currency or no price. |
| WF-28 | High | Math | **"Original length" is the latest harvest SE for the bucket ID across its whole history.** Bucket IDs are reused and one bucket has harvest SEs at several lengths, so the result is effectively arbitrary. Reversed comparisons get counted as downgrades. | downgrades.py:70-73. **OPL-2026-00037 / Moonwalk / bucket 27LKAB:** picked at 62cm. Harvest history 42cm@09-04, 62cm@09-03, 42cm@09-03, 62cm@09-03, 52cm@09-03. "Original" = 42cm, so it is shown as a downgrade although 62 > 42 (an upgrade). Bucket f1de7c (OPL-00001) has harvests at both 62 and 52. 30 of 32 flagged lines are dropped by the equality rule. | The original length is the graded length of the bucket's *current life*: the latest Grading/Receiving entry with `creation <= pli.creation` (or the Shelf Item length at allocation). A downgrade requires `orig_cm > pick_cm` numerically, not `!=`. |
| WF-29 | High | Filter | **The rose filter is a flat item_group comparison** (same bug as WF-17), so Spray and Standard always return 0. | downgrades.py:37-40. Sept spray: shown 0 lines. The real line is OPL-2026-00043 Good mood, whose item_group is "Spray Roses - Premium". | Use the subtree. |
| WF-30 | Med | Filter | **Reason and owner filters run after the 2,000-row cap,** so a filtered view can miss rows once data grows. "Owner" is `opl.owner`, the user who created the OPL (an allocator), but the column is labelled "picked by". The comment (:20) says "Receiving/Late Receipt" while the code uses Harvesting. | downgrades.py:84, 97-137. | Push reason and owner into SQL before the LIMIT. Take the actor from the FPL/issue scan user. |
| WF-31 | Med | KPI | **"Available stems" KPI** = Σ `available_stems_of_exact_length`. That is a snapshot taken at allocation and is 0 on all 383 rows, so the KPI is always 0. | `SUM(available_stems_of_exact_length>0)` = 0. | Remove it, or compute it live from shelf stock at the original length. |
| WF-32 | Med | Filter | **Drafts and cancelled OPLs are not excluded, and the date is `OPL.date_created`** (6 OPLs have it NULL). This is the same population problem as WF-7/WF-8. | downgrades.py:75-81. | Use docstatus < 2 and the shared date semantic. |
| WF-33 | Low | Math | **Foregone can be negative** when the sold rate is above the list rate, and negatives offset positives in the total. Valuation uses today's price and FX rather than the pick date. | downgrades.py:267-270, 213, 291. | Clamp at 0, or show a gain separately. Value at the pick date. |

### Performance profile
- **Queries:** one main query with a correlated subquery per row (indexed by `custom_bucket_id`, but it scans all harvest SEs for the bucket — about 10-70 per bucket), plus Price List, Item Price, and one FX query per currency.
- **Measured:** Sept 0.70 s with 32 candidate lines. This grows with candidates × harvest rows per bucket.

### v2 backend design notes
- **Currency:** take it from `soi.parent → Sales Order.currency`.
- **Original length:** get it from the bucket's grading/shelf record for the life the pick used. Ideally, store `original_length` on the Pick List Item at allocation time; sales_allocation already writes `stem_length` and `downgrade_reason` there.
- **Definition:** downgrade = numeric `orig_cm > pick_cm`. Foregone = (rate@orig − rate@pick) × stems, at the price valid on the pick date, in SO currency, plus a KES rollup at that date's FX rate.
- **Filters:** all of them (rose subtree, team, reason, actor) go into SQL before LIMIT.

---

### Cross-area notes
- **Flat item_group rose filter.** The same bug appears in Production, Discards and Downgrades (3 APIs). Only dashboard.py uses `resolve_rose_item_groups`. Check other areas (avails, coldroom…) for the same pattern.
- **`Sales Order Item.custom_opl` (no_copy=0).** The contamination also feeds sales_allocation.py:360 and loading_plan.py:68-75.
- **Stale stored values.** `Sales Order.custom_total_boxes` is stale on 74 of 368 Sept SOs; `OPL.custom_total_stems` is stale on OPL-00048.
- **Test records in live tables.** Box Labels TEST-* and SIM-BUCKET-* data are mixed into live KPIs.
