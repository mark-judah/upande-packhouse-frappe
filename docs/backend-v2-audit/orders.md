# Orders (area OR)

Scope: Order Summary, Order Fulfilment, Allocation Planning. Read-only analysis on kaitet.local.
APIs were run read-only via a scratch harness (frappe.connect, call, rollback). Paths below are relative to
`apps/upande_packhouse/upande_packhouse/`.

Ground truth used (verified on data): `soi.stock_qty` = line pack rate x boxes for all 1,317 submitted SOIs
(0 mismatches), so `stock_qty` is the correct "stems ordered". Canonical order boxes = `_set_order_summary`
dedup (spec line+length, then bunch group, then mix group, else line). Its results per delivery date are
listed in OR-A6.

### Cross-cutting data-integrity finding (affects Order Summary first)

| ID | Sev | Type | Finding | Evidence | Fix direction |
|---|---|---|---|---|---|
| OR-X1 | Critical | Integrity | `Sales Order Item.custom_opl` is not a reliable link. Of 227 SOIs with `custom_opl` set, **103 point to an OPL that no longer exists** and **56 point to an OPL that belongs to a different Sales Order** (OPL names were re-issued after deletes, and the SOI link was never cleared). Order Summary joins `opl.name = soi.custom_opl` and never checks `opl.sales_order = soi.parent`. | SQL: `SELECT COUNT(*), SUM(opl.name IS NULL), SUM(opl.sales_order<>soi.parent) FROM tabSales Order Item soi LEFT JOIN tabOrder Pick List opl ON opl.name=soi.custom_opl WHERE custom_opl<>''` → 227 / 103 / 56. Example: SAL-ORD-2026-00181 line 2 (Madam Cerise, 400 stems, delivery 2026-09-18) → OPL-2026-00035, which belongs to SAL-ORD-2026-00099 (Moonwalk). Order Summary shows that line as **"Dispatched", 890 dispatched, 3 boxes loaded**. In reality it has no pick list, no pack and no DN. | Use the OPL's own keys as the link: OPL.sales_order plus the Pick List Item `sales_order_item` (or OPL.mix_group/bunch_group). Alternatively, add `AND opl.sales_order = so.name` to every join. Clean the stale `custom_opl` values and clear them in the OPL on_trash/on_cancel. |

## Page: Order Summary (/order-summary-v2) — API: api/order_summary.py fetchOrderSummaryData, getOrderSummaryFilterOptions, getPackhouseLocations

### What the page shows and how it's computed
- The API returns one row per SOI for a single `delivery_date`. Ordered = `soi.stock_qty`. Confirmed = Confirmed Stems per SOI. Allocated and issued = Pick List Item `stock_qty` per (OPL, SOI).
- Everything from packed onward is **per OPL**, joined through `soi.custom_opl`:
  - packed stems/boxes come from the FPL (`pack_list_item`);
  - box labels and staged come from Box Label `staged=1` (count);
  - loaded = COUNT of Loading Sheet Items;
  - dispatched = DN Item `stock_qty`, grouped by the SOI's `custom_opl`.
- The client (www/order-summary-v2.html):
  - `raw()` converts staged and loaded boxes to stems as boxes x `pack_rate`, where `pack_rate` = `soi.custom_packrate`.
  - `agg()` sums ordered, confirmed, allocated and issued per line, and counts packed, staged, loaded and dispatched once per `opl_id`.
  - `chain()` caps each stage at the previous populated one and flags inversions.
- KPIs: Stems ordered, Fulfilment (dispatched ÷ ordered), At risk (lines due today and not fully staged), Late.
- Filters: location, farm, item group, team, the stage bucket and search are all client-side. Only the date goes to the server.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| OR-S1 | Critical | Math/KPI | Cross-linked `custom_opl` (OR-X1) puts other orders' box labels, loading and dispatch on lines that have none. The **Fulfilment KPI is inflated** and line **status is wrong**. | api/order_summary.py:158, :206-237 (joins only on `opl.name`). Delivery 2026-09-18: the page sums dispatched = **2,850 stems** (OPL-35 890 via SO-181, OPL-34 900 via SO-126, OPL-44 840 via SO-183, OPL-43 220 via SO-162). Real DNs against 09-18 orders = **0**. Statuses shown for 09-18 lines that have no pick list or pack: SO-181 ln2 "Dispatched", SO-126 ln2 "Dispatched", SO-126 ln3 "Loaded" (OPL-17 is SO-073's), SO-162 "Dispatched", SO-176 ln1-4 "Staged" (TEST-BOX-FULLCHAIN-01 of SO-095). | Join packing, labels and dispatch by `OPL.sales_order = so.name` and by the OPL's line or group. Dispatched should come from DN Item `so_detail = soi.name`, which is per line, exact and needs no OPL. |
| OR-S2 | High | Integrity/Status | A dangling `custom_opl` (OPL deleted) gives status **"Partially Allocated"** (the `else` branch at :339, because `opl_docstatus` is NULL). At the same time the client bucket puts the line in "Not allocated" (allocated=0). The status text and the tab disagree. | 2026-09-19: **100 of 316 lines** have `opl_id` set but no OPL row. All 100 show "Partially Allocated" with 0 allocated. | Treat "no live OPL for this SO line" as Not Allocated. Derive allocation from Pick List Items, not from `custom_opl`. |
| OR-S3 | Critical | Math (mixes) | Staged and loaded stems for **mixed box and mixed bunch lines are always 0**. The client multiplies boxes by `r.pack_rate` (= `soi.custom_packrate`), which is NULL for mixed lines. Even with `custom_packrate_mixed_box`, `agg()` keeps only the **first** line of the OPL, so you get boxes x one variety's per-box stems, which undercounts by the number of varieties. | html:136-144 (`staged: boxes*rate`). The API selects `soi.custom_packrate AS pack_rate` (api:99). SO-2026-00100 (mixed bunch, 6 varieties, 09-16): `pack_rate=None` on all 6 lines, so staged and loaded can never show above 0. The real box BOX-OPL-2026-00036-1 holds 120 stems (6x20). | Staged, loaded and dispatched stems should come from Box Label content: SUM(`Box Label Item.qty` x the UOM's bunch size), or `Box Label.pack_rate`, which already stores the actual stems in that box. Group per variety for line rows and per OPL for roll-ups. Never use boxes x pack rate. |
| OR-S4 | High | Math | Staged stems for **under-packed boxes are overstated**. Box count x the line's nominal pack rate ignores boxes closed short. | SO-2026-00252 ln1 Odilia (2026-09-20): 3 staged labels x 300 = **900**, flagged red and capped to packed 480. Real staged stems are 300+170+10 = **480** (BOX-OPL-2026-00045-1..3 `pack_rate` 300/170/10; FPL-2026-00013 boxes 2-3 have under_pack_reason "End of day / cutoff"). | Same as OR-S3: sum the stems on the actual Box Labels. |
| OR-S5 | High | Math | The **staged flag is cleared when a box is loaded**, so "staged" undercounts and loaded > staged, which triggers the red inversion. `chain()` then caps loaded and dispatched at the staged value. | Box Label: 2 labels have `staged=0, loaded=1` (BOX-OPL-2026-00034-2/-3). SO-2026-00099 ln2 Athena (09-16): staged 1 box = 300, loaded 3 = 900, dispatched 900. The strip shows loaded **300** and dispatched **300** (red), but the line is fully dispatched (MAT-DN-2026-00012). | Stage counts must be cumulative: staged = `staged OR loaded OR delivered`, loaded = `loaded OR delivered`. |
| OR-S6 | High | Math/Status | "Loaded" counts **Loading Sheet Item rows**, on sheets of any status (all 3 Loading Sheets are docstatus 0, two still "Loading"), not Box Label `loaded`. | api:216-224. BOX-OPL-2026-00025-1 is on LS-2026-09-15 (status Loading) with `loaded=0, staged=0`. Order Summary shows SO-2026-00079 ln1 as **"Loaded"** (1 loaded box) while the box is not even staged. The `/* Loaded: Loading Sheet Items with loaded=1 */` comment is false (the column doesn't exist). Also the `/* Staged: loaded=1 */` comment at :131 is wrong. | Loaded = Box Label `loaded=1` (or `delivered=1`). Optionally require a departed Loading Sheet for "dispatched to truck". |
| OR-S7 | Med | Integrity/Math | Box Labels are counted with no guard. Labels not created from an FPL (test or manual, `farm_pack_lis IS NULL`) inflate `box_labels_count`, which drives status (Staged vs Partially Staged, Loaded vs Partially Loaded). | 10 of 44 labels have no FPL. OPL-2026-00043 (SO-250 ln1, 1 box ordered, 1 packed) has **7** labels (TEST-STAGE-BOX-*, TEST-R2T-BOX-*), so box_count is 7. | Count labels `WHERE farm_pack_lis IS NOT NULL` (FPL not cancelled), or count FPL box_ids. |
| OR-S8 | High | Math (mixes) | Packed, box-label, staged, loaded and dispatched values are **per-OPL figures repeated on every variety line** of a mix or bunch group. On variety rows this (a) shows the whole box's stems against one variety, flagged red as "exceeds previous stage", and (b) sets server status "Packed" when OPL packed ≥ **line** ordered, even if only one variety is packed. | api:183-203 (GROUP BY `order_pick_list`), :328. SO-2026-00100 (09-16): every variety line shows packed **120** against ordered 20 / issued 20, so all 6 rows are red. The legend at html:106 admits "a variety row can read further along than that variety alone has travelled". | Aggregate FPL `pack_list_item` per (OPL, item_code, length) for variety rows, and per OPL for group rows. Status per line from its own variety. |
| OR-S9 | Med | KPI | "At risk" and "Late" count **lines**, not orders. Because of OR-S3, every mixed line due today is "at risk" even after staging. "Late" can only be non-zero when the user picks a past date, since the page loads one delivery date. | html:400-401. | Count orders (or OPL groups). Use corrected staged stems. Late = open orders with `delivery_date < today` from a separate query, not limited to the selected date. |
| OR-S10 | Med | Math (boxes) | **No boxes figure is shown above line level.** Each mixed or spec variety line shows the group's `custom_number_of_boxes` ("5 bx" on every colour). The CSV exports that per-line value, so summing the column overcounts. `boxes_packed` and `box_labels_count` are returned but never displayed. | html:337, :440. Per-line sum vs canonical boxes: 09-24 **643 vs 307**, 09-18 719 vs 417, 09-19 658 vs 455, 09-15 102 vs 55. | Show boxes ordered per order/customer from the canonical dedup (or a fresh `so.custom_total_boxes`, see OR-A6), and show packed/staged/loaded **boxes** from Box Labels next to stems. |
| OR-S11 | Low | Math | Takt uses `MAX(pli.modified)` and `MIN(bl.modified)`. Any later edit to a row moves the timestamps. | api:254-265. | Record `issued_at` and `staged_at` timestamps, or use the Version log. |
| OR-S12 | Low | Filter | The location, farm and team filters are client-side and match only lines with a live OPL, so unallocated lines disappear when a location is picked. The server's `location` param is never sent. The farm filter and the location derivation inherit OR-X1, so cross-linked lines get another order's farm and team. | html:208-216, :270. | Filter on the server, using OPL.farm for allocated lines. Decide explicitly whether unallocated lines appear under "all locations" only. |

### Performance profile
- 1 SQL per load, plus 3 master calls on first paint. The page polls every 60 s.
- Measured 9-20 ms on current data. Seven derived tables (`pli_agg`, `bl`, `ls`, `dsp`, `iss`, `stg`, `inv`) aggregate **entire tables with no date restriction**, so cost grows with history, not with the day.
- Missing indexes (SHOW INDEX via information_schema): `tabSales Order.delivery_date`, `tabSales Order Item.custom_opl`, `tabPick List Item.sales_order_item`, `tabFarm Pack List.order_pick_list` and `.sales_order`, `tabLoading Sheet Item.box_label_link`, `tabSales Invoice.custom_so`, `tabConfirmed Stems.sales_order_item`. `tabBox Label.order_pick_list` is indexed.
- `getOrderSummaryFilterOptions` runs DISTINCT over every SOI.item_group. It is cheap now and cacheable.

### v2 backend design notes
- Drive everything from the day's SOI set: `so.delivery_date = d`, using an index on `(delivery_date, docstatus)`. Push the date filter into every aggregate with `opl.sales_order IN (day's SOs)`.
- Ordered stems = `soi.stock_qty`. Order and customer boxes = canonical dedup.
- Allocated and issued = Pick List Item per `sales_order_item` (outstanding vs issued).
- Packed = FPL `pack_list_item` stems per (OPL, item_code, length), with FPL docstatus ≠ 2.
- Staged, loaded and delivered stems and boxes = Box Labels that have an FPL, with cumulative flags and stems from `box_item` or `pack_rate`.
- Dispatched = DN Item via `so_detail`, DN docstatus = 1.
- Return roll-ups from the server (per order and per customer), computed once with OPL and group dedup, so the client does no unit conversion.
- Cache the masters for 1 h.

## Page: Order Fulfilment (/order-fulfilment-v2) — API: api/order_fulfilment.py getOrderFulfilment

### What the page shows and how it's computed
- One row per SOI for a delivery date:
  - ordered = `qty*conversion_factor` (= stock_qty on all rows; verified);
  - confirmed = Confirmed Stems for the line;
  - packed = SUM FPL `pack_list_item.stock_qty` for the **same SO + item_code + length**;
  - transferred = Pick List Item rows awaiting transfer or shelved;
  - opl = MIN OPL of the SO.
- The client sums lines per account manager and per order. Fulfilled % = packed ÷ ordered.

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| OR-F1 | Critical | Math (mixes) | Packed is matched by **variety + length within the SO**, not by line. When a variety/length appears on more than one line (two straight lines, or the same colour in a mix group and a bunch group), **each line gets the full packed total**, so order, manager and day packed are double-counted. | api/order_fulfilment.py:37-45. SO-2026-00380 (09-24): ln1 Snowflake 52cm (600 ordered, OPL-47 packed 600) and ln2 Snowflake 52cm (500 ordered, never picked) **both show packed 600**. Order fulfilment shows 1,200/1,100 = **109%**; the truth is 600/1,100 = **55%**. The day's "Packed" KPI shows 2,520; the truth is 1,920. Mixes are at risk: SO-2026-00379 repeats Aqua/Athena/Furiosa/Tropical Amazon/Moonwalk across mix group 1 and bunch groups ::5/::7, so each gets every box's stems once packed. | Packed per line = FPL items of the line's **own OPL** (OPL.sales_order = SO and the OPL covering this SOI), split by item_code+length within that OPL. Better: store `sales_order_item` on Farm Packlist Item when the FPL is built from the guide. |
| OR-F2 | Med | Math | When `soi.custom_length` is NULL or empty, the length clause matches **every length** of that variety in the SO. | api:44, :51. | Exact length match, plus explicit handling of NULL. |
| OR-F3 | Med | KPI | "Fulfilled" is defined as packed ÷ ordered, not dispatched. Over-pack is not capped per line, so one line's surplus hides another's shortfall in roll-ups. | html:26-27, render(). | Report packed% as Σ min(packed_line, ordered_line) ÷ Σ ordered, plus a separate dispatched%. |
| OR-F4 | Low | Integrity | The `opl` column is `MIN(OPL)` of the SO. Multi-OPL orders show one arbitrary OPL on every line (SO-103 ln5 shows OPL-40, but its OPL is OPL-41). | api:26. | Return the line's own OPL from Pick List Item `sales_order_item`. |
| OR-F5 | Low | KPI | `transferred` counts PLI rows by variety+length, so it has the same duplication as OR-F1. It is only used as a link toggle. | api:46-53. | Per line. |

### Performance profile
- 1 SQL with **4 correlated subqueries per SOI row**: OPL MIN, Confirmed Stems, FPL⋈items, PLI⋈OPL. That is about 4×N sub-executions (316 lines on 09-19 gives about 1,260).
- `tabFarm Pack List.sales_order` and `tabOrder Pick List.sales_order` have no index, so each subquery scans. Measured 7-40 ms today (16 FPLs, 55 OPLs). It will degrade linearly with history × lines.

### v2 backend design notes
- One pass: the day's SOIs LEFT JOIN pre-aggregated derived tables keyed by `sales_order_item`:
  - Confirmed Stems GROUP BY `sales_order_item`;
  - PLI GROUP BY `sales_order_item`;
  - FPL items GROUP BY (OPL, item_code, length), mapped to SOI through PLI/OPL.
- Index `Order Pick List.sales_order`, `Farm Pack List.sales_order` and `.order_pick_list`.
- Reuse the same line-level fact query as Order Summary (one "order line pipeline" service powering both pages).

## Page: Allocation Planning (/sales-allocation-planning-v2) — API: api/allocation.py fetchSalesAllocationPlanningData, confimSalesOrderItem; page/sales_allocation/sales_allocation.py default_delivery_date

### What the page shows and how it's computed
- The API builds order lines (`stems_ordered = soi.custom_ordered_quantity`) and **CROSS JOINs every Farm**, then LEFT JOINs shelf stock = SUM(`Shelf Item.stem_qty`) per (variety, length, farm).
- It computes `stock_status`, and cover % = stock ÷ ordered, with `LIMIT 3000`.
- Confirmed Stems are attached by a second query. The server `aggregations` are returned but the v2 page **does not use them**.
- The client groups rows into lines. KPIs:
  - Order lines;
  - Ordered stems (Σ `stems_ordered`);
  - boxes ("to pack", deduped by `boxGroupKey`: bunch group, then mix group, else line);
  - Available stock (Σ `stock_total` over every line × shown farm);
  - Confirmed (Σ entries, % of ordered).
- Per farm cell: stock, balance = stock − ordered, Sufficient/Partial status.
- The only page helper used is `default_delivery_date`, which is correct (server tomorrow).

### Findings
| ID | Severity | Type | Finding | Evidence | Correct definition / fix direction |
|---|---|---|---|---|---|
| OR-A1 | Critical | Math/Perf | **`LIMIT 3000` on lines × 16 farms silently drops order lines.** Any day with more than 187 lines is truncated: lines are lost and the last line is cut mid-farm. All KPIs are computed on the truncated set. | api/allocation.py:96-108 (CROSS JOIN all_farms), :153. Page vs truth: **09-18** 188 lines / 108,685 stems / 247 boxes vs **236 / 136,725 / 417**. **09-19** 188 / 55,260 / 153 vs **316 / 198,379 / 455**. **09-24** 188 / 67,770 / 168 vs **248 / 113,743 / 307**. The last line on 09-18, 09-19 and 09-24 has only 8 of 16 farm rows. | Return lines and stock as two result sets (lines once; stock per (variety, length, farm) once) and join client-side. No cross join, no LIMIT, or page by line. |
| OR-A2 | Critical | Math | **Ordered stems uses `custom_ordered_quantity`, which is stale** (0 on older rows) instead of `stock_qty`. | api:83. Mismatches on open SOs: 09-02/03/04 **all 172 lines = 0** (true 84,264 stems). **09-20: 50 of 155 lines**, so the page shows **17,100** stems vs true **32,980**. 09-19: 12 lines, 169,119 vs 198,379. 09-16: SO-2026-00099 both lines 0 vs 900. | `stems_ordered = soi.stock_qty` (= line pack rate × boxes, verified on all 1,317 SOIs). Backfill `custom_ordered_quantity`, or drop the field. |
| OR-A3 | Critical | Write logic | `confimSalesOrderItem` caps confirmation at **`custom_ordered_quantity`**. For stale lines the cap is 0, so **no farm can confirm the line**, and it throws "Maximum available: 0". The client cap (`remaining()`) uses the same stale value and shows "Up to 0". | api:339, :352-357. Example: SO-2026-00099 ln1/ln2 (coq 0, stock_qty 900). | Cap at `stock_qty`. Validate on the server against a fresh read of the line. |
| OR-A4 | High | Math | **"Available stock" is double-counted.** The same farm/variety/length stock is added once per order line that wants it, and summed across all farm columns. The KPI exceeds total physical stock. | html:235; api:56-70. 09-18 KPI = **855,803 stems**, but **all** shelf stock on site is only **606,241** (Σ Shelf Item stem_qty > 0). | Available = Σ over DISTINCT (variety, length, farm) needed by the visible lines. Present demand vs supply per variety/length, not per line. |
| OR-A5 | High | Math | Stock is raw `Shelf Item.stem_qty`. It ignores **outstanding allocations** (Bucket Allocation Status `allocated_quantity`), **pending discards**, the **age limit** and **cooling hours**, all of which the allocator (`sales_allocation.py` ~:680-730, `DISCARD_EXCLUSION` :27) applies. The planning page therefore promises stock the allocator will refuse. | Site totals: raw 606,241. Net of outstanding allocations: 586,857 (19,384 stems already reserved). With the allocator's discard exclusion: **0**, because an approved, un-executed Discard Request covers 3,798 buckets (probably test data, but it proves the two definitions diverge). Per-line balance/"Sufficient" also ignores that two lines compete for the same stock. | Reuse the allocator's availability query: `GREATEST(0, stem_qty − bas.allocated_quantity)` with dedup per bucket, minus discard-pending, age ≤ the farm's max, cooled. Show balance per variety/length as Σ demand vs available. |
| OR-A6 | High | KPI (boxes) | The page's box rule (`boxGroupKey`, html:125-131; same rule at api:200-226) **ignores the spec key** (`custom_line` + length). That key is checked first by the canonical `_set_order_summary`, and the canonical rule also groups by bunch/mix group regardless of the mixed flag. Spec fills that mix bunch groups and mix groups are overcounted. | Full day, client rule vs canonical vs stored `so.custom_total_boxes`: 09-24 **395 vs 307** (stored 306); 09-15 65 vs 55; 09-13 6 vs 5; 09-18 421 vs 417. Worst order: **SAL-ORD-2026-00379: 152 vs 72** (spec TSTSPEC 408 lines in mix group 1 and bunch groups ::5/::7). `custom_total_boxes` is stale too: 09-18 stored 719, 09-19 545 vs 455, 09-15 102 vs 55. | Use the exact `_set_order_summary` key order. Recompute `custom_total_boxes` for all open orders (or compute live), then use it for order totals. |
| OR-A7 | Med | Filter | The "Stock farm" filter only hides columns. The server `farm` param is never sent, and the CROSS JOIN still returns all 16 farms (including zero-stock and non-packhouse farms). | html:217, :145. | Send the farm to the server, list only farms with stock or confirmations, and keep the column filter client-side. |
| OR-A8 | Med | KPI | The cover % and Sufficient/Partial status use stale `stems_ordered` (OR-A2). When ordered = 0, cover shows 0 and the status is "Sufficient" for any stock ≥ 0. | api:142-150. | Base on `stock_qty`. |
| OR-A9 | Low | Write logic | Confirmation is per variety line with no check that a mixed box or bunch group is confirmed consistently (same farm, all colours), and no check of bunch/box multiples. The whole SO is loaded and saved (`ignore_validate_update_after_submit`) per click, plus a second full `get_doc` for the bookings. | api:315-405, :436. | Write only the Confirmed Stems child (insert/update via `frappe.get_doc("Confirmed Stems")` or a dedicated doctype) and validate per group. |
| OR-A10 | Low | Perf/Dead code | The CTE `confirmed_stems` (api:109-122) is never used, and the server `aggregations` (by_farm sums lines × farms without dedup, so it is wrong anyway) are unused by v2. | — | Remove. |

### Performance profile
- 2 SQL per load. The main query aggregates **every Shelf Item** (no variety filter) and produces lines × farms rows (2,480-3,000 rows, of which about 90% are empty farm cells). It is then truncated.
- Measured 70-155 ms. There is no index on `Shelf Item(variety, stem_length)` or `Sales Order.delivery_date`. The page polls every 60 s.
- `confimSalesOrderItem`: 2 full SO `get_doc`, 1 full-document save and commit per click.

### v2 backend design notes
- Lines: day's SOIs with `stock_qty` (stems), the canonical box key, and the group key (mix/bunch/spec) for grouping in the UI.
- Supply: one aggregate over the allocator's availability definition, restricted to `(variety, length) IN day's lines`, GROUP BY (variety, length, farm). Indexes needed: `Shelf Item (variety, stem_length)` and `Bucket Allocation Status (bucket_id, item_code, stem_length)`.
- Demand vs supply at (variety, length) level: Σ ordered stems vs Σ available, plus confirmed per farm.
- KPIs computed on the server from those sets, never from a truncated cross product.
- Confirmations: store them per SOI (they already are). Validate against `stock_qty`, and expose a group-level confirm for mixes.
