"""Patch for the live `Shelving Entry` Server Script (API: createShelvingEntry).

Shelving currently writes `Shelf Item` rows and nothing else, so a bucket
trucked from Simotwo to a Kapkolia shelf keeps `warehouse = Simotwo Receiving
Cold Store` forever while `Shelf.farm` says Kapkolia. This snippet posts the
arrival hop of the `SO Warehouse Mapping` route, so the shelf row records where
the stems really are.

Drop the two blocks below into the server script — everything else there stays
as it is. `frappe.call` is available inside the Server Script sandbox; if this
site's sandbox rejects it, install the hook as a `Shelf` doc event instead and
call `upande_packhouse.stock_movement.post_arrival` directly.

--------------------------------------------------------------------------
1. Resolve the business unit once, near the top of the main handler
--------------------------------------------------------------------------

    business_unit = data.get("business_unit") or "Roses"

--------------------------------------------------------------------------
2. Replace the body of the shelving loop
--------------------------------------------------------------------------

Current:

    for ri in receiving_doc.items:
        new_item = shelf_doc.append("items", {})
        new_item.bucket_id   = bucket_id
        new_item.variety     = ri.item_code
        new_item.date_added  = frappe.utils.now_datetime()
        new_item.stem_length = stem_length
        new_item.custom_stem_length = stem_length
        new_item.stem_qty    = ri.qty
        new_item.greenhouse  = ri.s_warehouse
        new_item.warehouse   = ri.t_warehouse
        total_qty += (ri.qty or 0)

Replacement:

    for ri in receiving_doc.items:
        arrival = frappe.call(
            "upande_packhouse.stock_movement.post_arrival",
            bucket_id=bucket_id,
            item_code=ri.item_code,
            qty=ri.qty,
            source_warehouse=ri.t_warehouse,
            business_unit=business_unit,
            farm=farm,
            stem_length=stem_length,
        )

        new_item = shelf_doc.append("items", {})
        new_item.bucket_id   = bucket_id
        new_item.variety     = ri.item_code
        new_item.date_added  = frappe.utils.now_datetime()
        new_item.stem_length = stem_length
        new_item.custom_stem_length = stem_length
        new_item.stem_qty    = ri.qty
        new_item.greenhouse  = ri.s_warehouse
        new_item.warehouse   = arrival["warehouse"]      # ← where the stems now are
        total_qty += (ri.qty or 0)

        result.setdefault("arrivals", []).append(arrival)

--------------------------------------------------------------------------
Notes
--------------------------------------------------------------------------
* `post_arrival` stops before the terminal hop — shelving is an arrival, not a
  sale. The Graded Sold leg is posted by the allocation page.
* It is safe to re-run: each leg is skipped when this bucket's own ledger
  balance in the source warehouse is already zero (Bin is not consulted — a
  cold store holds thousands of buckets).
* A bucket shelved at its own farm (Kapkolia onto a Kapkolia shelf) resolves to
  an empty route and moves nothing.
* Two unrelated bugs live in the same loop and are worth fixing while you are
  in there:
    - `mark_bucket_as_shelved()` flags only `receiving_doc.items[0]`, while this
      loop shelves every item row — multi-variety spray-rose buckets end up
      half-flagged.
    - `stem_length` is hardcoded to None ("stem length not tracked on this
      site"), so every newly shelved bucket lands with a null length and can
      never satisfy an exact-length allocation.
"""
