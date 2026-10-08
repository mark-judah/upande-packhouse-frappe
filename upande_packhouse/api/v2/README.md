# Packhouse v2 backend — one definition per number

The v2 pages (`www/*-v2.html`) read from endpoints in this package. Every
endpoint computes its numbers through `api/v2/core`, so two pages that show the
same quantity always agree. The v1 APIs (`api/*.py`) are untouched and keep
serving the v1 pages until the swap-over.

Findings referenced in comments (WF-1, OR-S3 …) are in `docs/backend-v2-audit/`.

## Shared definitions (core)

| Number | Function | Shown on |
|---|---|---|
| Ordered stems / bunches per line | `pipeline.fetch_lines` → `ordered_stems`, `ordered_bunches` (`units`) | Workflow, Order Summary, Order Fulfilment, Allocation Planning, Scheduler, Stock Visibility, Avails, Sales Order |
| Boxes per order | `boxes.order_boxes` / `pipeline.order_rollup().boxes` | Workflow, Order Summary, Allocation Planning, Sales Order list + editor |
| Line → pick list | `pipeline.attach_pipeline` (Packing Guide + Pick List Item, never `custom_opl`) | everything order-side |
| Confirmed / allocated / outstanding / picked / issued stems | `pipeline.attach_pipeline` | Workflow, Order Summary, Fulfilment, Allocation Planning, Scheduler, Cold Room |
| Planned boxes / stems (Packing Guide) | `attach_pipeline` → OPL `planned_boxes`, `planned_stems` | Workflow, Order Summary, Scheduler |
| Packed stems / complete boxes | `attach_pipeline` → line `packed_stems`, OPL `complete_boxes` | Workflow, Order Summary, Fulfilment, Stem Movement |
| Staged / loaded / delivered stems + boxes | `attach_pipeline` (labels from a pack list, cumulative flags) | Order Summary, Workflow, Stem Movement |
| Dispatched stems | `attach_pipeline` (Delivery Note Item by `so_detail`, no returns) | Order Summary, Fulfilment, Stem Movement |
| Line stage | `pipeline.line_stage` | Order Summary, Workflow tiles |
| Stock on shelf, split held / allocated / free / allocatable | `stock.bucket_rows` + `stock.summarize` / `stock.totals` | Stock Visibility, Avails, Cold Room, Allocation Planning, Stem Movement (on shelf), Bucket Journey |
| Stock age | `stock.bucket_rows().age_days` (harvest date, else shelving date) | same as above |
| Spray / Standard | `rose.rose_sql`, `rose.rose_type` | every page with a rose filter |

New shared modules added by the page work must live in `core/` and be the only
implementation of their number (e.g. `core/harvest.py` for harvested stems,
`core/pricing.py` for a price on a date, `core/transfer.py` for a bucket's
transfer stage).

## Endpoint rules

1. `@frappe.whitelist()` functions in `api/v2/<page>.py`, named `get_<thing>`
   for reads. Reads never write or commit (audit TR-14). Writes are separate
   POST functions and say so in their name (`save_`, `confirm_`, `set_`).
2. Return `{"success": True, ...}`; on a handled problem
   `{"success": False, "error": "<sentence>"}`. Numbers are plain floats/ints
   in stems unless the key says otherwise (`*_boxes`, `*_bunches`, `*_buckets`).
3. Filters are applied in SQL before any limit. If a limit exists, return
   `truncated: true` and the full count. KPI totals are computed over the full
   filtered set, never over a truncated list.
4. One date meaning per page:
   - packing and order pages: the Sales Order's `delivery_date`
     (pick lists inherit their order's delivery date);
   - harvest pages: harvest `posting_date`;
   - discards: approval date (`COALESCE(approval_date, requested_date)`).
5. Orders counted are submitted (`docstatus = 1`) unless a page explicitly
   shows drafts, in which case they are returned and labelled separately.
   Cancelled documents never count.
6. Farm means the SHELF's farm for stock, the pick list's farm for packing.
   **Every KPI follows every filter on its page** (changed 2026-10-07). When a
   farm or region filter is set, order-side figures (ordered, stems to deliver,
   boxes, packed, dispatched…) are scoped by the ORDER's farm —
   `core/region.ORDER_FARM_SQL`:
   COALESCE(Sales Order.farm, Sales Order.custom_farm, Order Pick List.farm).
   (Amended 2026-10-07: pick lists say Kapkolia even for Karen orders and ~93%
   of demand has no pick list, so scoping by pick list farm emptied Karen and
   dropped most demand.) Orders with no farm anywhere drop out while a
   farm/region filter is on. The bucket's ORIGIN farm (`Pick List Item.farm`,
   shelf farm) is a different dimension — use it only for stock, transfer and
   "where the stems came from" figures, and label it so. A
   filter that a KPI genuinely cannot honour must be stated next to the KPI
   (e.g. "all farms"), never silently ignored.
6a. Region (universal Ravine / Karen filter): `region` arg on every endpoint
   that has a farm notion, resolved only through `core/region.farms_for()`
   and applied on the same column as that page's farm filter.
6b. Every data page accepts `from_date` / `to_date` and can be read for any
   past range (not just today/forward). The date column is the page's one
   date meaning from rule 4.
7. Roll-ups (per order, per customer, per farm, KPIs) are computed on the
   server. The browser formats; it does not add up stems or convert units.
8. No `Sales Order.custom_total_boxes`, `custom_ordered_quantity`,
   `Sales Order Item.custom_opl`, `Order Pick List.custom_total_stems`,
   `Pick List Item.custom_box_label` or `Bucket QR Code.status` as a source of
   a number (all stale or unreliable — see the audit).

## Verifying against real data

Run a read-only script in the bench console with commits blocked, e.g.

```python
import frappe
frappe.db.commit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("commit blocked"))
from upande_packhouse.api.v2 import order_summary
r = order_summary.get_order_summary(delivery_date="2026-09-24")
frappe.db.rollback()
```

Known-good figures (kaitet.local) the core reproduces: ordered stems
18/19/20/24 Sep = 136,725 / 198,379 / 32,980 / 113,743; SO-2026-00380 packed
600 on line 1 only; SO-2026-00252 line 1 staged 480; stock on shelf 606,241
stems in 3,797 buckets on 2,198 shelves, available 0 (every bucket is on an
open discard request), 19,384 allocated stems inside held buckets.
