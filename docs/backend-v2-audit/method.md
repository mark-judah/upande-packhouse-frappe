# Packhouse backend audit — shared primer

App: /home/jk/Projects/upande-local-bench-v16/apps/upande_packhouse/upande_packhouse
Site: kaitet.local (MariaDB, real data: ~1,400 Sales Order Items incl. 663 mixed-box and 85 mixed-bunch lines, 55 OPLs, 17 FPLs, 44 Box Labels).

This is an ANALYSIS phase. Do not change any code. Produce findings only.

## Goal

The user is planning a "v2 backend" for the packhouse dashboards: faster queries by
design and mathematically exact numbers. Known problem reports from the user:
"total stems, total boxes, the KPIs, the filters" are wrong on many dashboards;
"stems were not considering mixes"; boxes are "not counted accurately based on the FPL".
Find every such defect, prove it, and describe the correct definition.

## The data model (what counts as truth)

### Ordered (Sales Order Item, SOI)
- Line shape: straight (`custom_mixed_box=0 AND custom_mixed_bunch=0`), Mixed Box
  (`custom_mixed_box=1`, grouped by `custom_mix_group`), Mixed Bunch
  (`custom_mixed_bunch=1`, grouped by `custom_bunch_group`). Spec-filled lines carry
  `custom_line` (a Specifications name).
- Stems-per-box of a line (`sales_order_engine._line_packrate`): mixed box / mixed bunch
  → `custom_packrate_mixed_box` (per-VARIETY stems in one box); straight → the integer
  NAME of the `custom_packrate` Link (Packrate name IS the number).
- **Line stems = line stems-per-box × `custom_number_of_boxes`.** Stems ARE additive
  across lines, mixed or not. `stock_qty` is set to this on save; `qty = stems / UOM
  factor` where UOM is "Bunch (N)" → N stems per bunch. `custom_ordered_quantity` is
  kept equal to stems on save (older rows may be stale).
- **Order boxes are NOT additive across lines.** Every line in one mix group / bunch
  group / spec fill describes the SAME physical boxes. Canonical dedup
  (`sales_order_engine._set_order_summary`): key = (custom_line, custom_length) if
  custom_line, else ("bunch", custom_bunch_group), else ("mix", custom_mix_group), else
  the line itself; count `custom_number_of_boxes` once per key. Stored result:
  `Sales Order.custom_total_boxes` / `custom_total_stems` (only fresh for orders saved
  after that code landed — verify).
- Packrate check: packing guide requires packrate % stems_per_bunch == 0.
- `qty` alone is in the line UOM (bunches) — summing `qty` across lines mixes units.
  Summing `custom_number_of_boxes` across lines overcounts mixed boxes.

### Planned / packed (Order Pick List → Packing Guide → Farm Pack List → Box Label)
- OPL: straight = one OPL per SOI; mixed box = one OPL per mix group; mixed bunch = one
  per bunch group (see page/sales_allocation/sales_allocation.py `_create_pick_list`).
  OPL child `table_ytkc` (Pick List Item) = allocated buckets/stems; `table_nade`
  (Packing Guide) = the plan.
- **Packing Guide** (`packing_guide.py`): one row per (box_number, variety): stems,
  bunches, pack_rate. Planned boxes of an OPL = COUNT(DISTINCT box_number). Planned
  stems = SUM(stems). This is the single source of truth for the box plan.
- **Farm Pack List** (FPL, `farm_pack_list.py`): `pack_list_item` rows = actual packed
  stems per box_id + item_code (`stock_qty`), with `under_pack_reason` for boxes closed
  short. Packed stems = SUM(stock_qty) over non-cancelled FPLs. A box is complete when
  every guide (box, variety) is met or the box has an under_pack_reason.
  FPL auto-submits when complete (`fpl_pack_blockers`).
- **Box Label**: one per physical packed box, created on FPL submit
  (`box_label.py`); flags staged/loaded/delivered; `pack_rate`, `box_total_count`,
  child `box_item` (variety qty). Packed/staged/loaded/dispatched box counts should come
  from here (or FPL box_ids), never from SOI `custom_number_of_boxes` sums.

### Stock side (read the code to confirm)
Buckets (`Bucket QR Code`), Shelf / Shelf Item, Shelving Log, Bucket Allocation Status /
Bucket Allocations (quantity_allocated; `cancelled` flag), Discard Request, Stock Entry
moves (`stock_movement.py`), Packing Reject / Packing Bypass logs. Learn how each API
uses them and whether cancelled/amended docs (docstatus=2) and cancelled allocations
are excluded.

## How to verify against real data (read-only)

Run SQL with:
  cd /home/jk/Projects/upande-local-bench-v16 && bench --site kaitet.local mariadb -e "SELECT ..."
SELECT / EXPLAIN / SHOW INDEX only. Never INSERT/UPDATE/DELETE/ALTER, never bench migrate,
clear-cache, set-config, console writes. Do not create sessions, users, passwords or API keys.

You may time a read-only API method with
  cd /home/jk/Projects/upande-local-bench-v16 && time bench --site kaitet.local execute <dotted.method> --kwargs "{...}"
ONLY after reading the method end-to-end and confirming it performs no writes
(no .save/.insert/.submit/db.set_value/db.sql UPDATE/commit, no enqueue). If unsure, don't.
To count queries, read the code (loops calling frappe.db.* / get_doc = N+1).

For every math finding, give evidence: the code (file:line), and when possible a real
record or date where the shown figure differs from the correct one (both numbers).

## Output

Write your findings to /tmp/claude-1000/-home-jk-Projects-upande-local-bench-v16-apps-upande-packhouse/3b04fa79-17dc-44f1-9724-ba4c7d076531/scratchpad/audit/<your-area>.md
in this structure, then reply with a 10-line summary:

```
# <Area>
## Page: <page name> (/page-v2) — API: <module.function>, ...
### What the page shows and how it's computed (brief)
### Findings
| ID | Severity (Critical/High/Med/Low) | Type (Math / Filter / KPI / Perf / Integrity) | Finding | Evidence (file:line + real-data proof) | Correct definition / fix direction |
### Performance profile
- queries per request (estimate from code), N+1 loops, full scans, missing indexes (SHOW INDEX), measured time if safe
### v2 backend design notes
- the correct source of truth per number, one-query/aggregate plan, indexes needed, caching
```

IDs: prefix with your area code (e.g. WF-1). Severity: Critical = wrong number users act on
(ordering/packing/dispatch decisions) or data corruption; High = wrong KPI/filter;
Med = edge-case wrong or slow (>1s); Low = cosmetic/minor.
Be concrete and skeptical: confirm each claim by reading the code path fully; mark
anything unverified as "unverified". Also check what the v2 frontend page sends/derives
(www/<page>-v2.html) — if the page does client-side math on top of the API, audit that too.
