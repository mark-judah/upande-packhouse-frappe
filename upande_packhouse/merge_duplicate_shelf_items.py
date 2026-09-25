"""Merge duplicate Shelf Item rows: same bucket + variety + stem length on one shelf.

Shelving used to write one Shelf Item per Receiving row, so a bucket received as
Odilia 30 + Odilia 40 landed as two rows. Every availability read LEFT JOINs
Bucket Allocation Status per row and subtracted the whole allocation from each,
so a fully allocated 70-stem bucket showed -40 and -30 on the allocation page.

    bench --site <site> execute upande_packhouse.merge_duplicate_shelf_items.run
    bench --site <site> execute upande_packhouse.merge_duplicate_shelf_items.run --kwargs "{'dry_run': False}"

Keeps the lowest-idx row with the summed qty, drops the others, and folds the
dropped rows' Shelving Log entries into the kept row's entry so the on-shelf
ledger still balances. Bucket Allocation Status is untouched: allocation already
summed duplicate rows when it created it.
"""

from collections import defaultdict

import frappe


def run(dry_run=True):
	rows = frappe.db.sql(
		"""
		SELECT si.name, si.parent, si.idx, si.bucket_id, si.variety,
		       COALESCE(si.stem_length, '') AS stem_length, COALESCE(si.stem_qty, 0) AS stem_qty
		FROM `tabShelf Item` si
		WHERE si.parenttype = 'Shelf'
		ORDER BY si.parent, si.idx
		""",
		as_dict=True,
	)
	groups = defaultdict(list)
	for r in rows:
		groups[(r.parent, r.bucket_id, r.variety, r.stem_length)].append(r)
	dups = {k: v for k, v in groups.items() if len(v) > 1}

	merged = 0
	stems = 0
	for (shelf, bucket, variety, length), items in dups.items():
		keep, drop = items[0], items[1:]
		total = sum(i.stem_qty for i in items)
		stems += total
		merged += 1
		print(
			f"{shelf} {bucket} {variety} {length}: {[i.stem_qty for i in items]} -> {total} (keep {keep.name})"
		)
		if dry_run:
			continue
		frappe.db.set_value("Shelf Item", keep.name, "stem_qty", total, update_modified=False)
		drop_names = [d.name for d in drop]
		frappe.db.delete("Shelf Item", {"name": ["in", drop_names]})
		frappe.db.delete("Shelving Log", {"shelf_item": ["in", drop_names]})
		kept_log = frappe.db.get_value("Shelving Log", {"shelf_item": keep.name}, "name")
		if kept_log:
			frappe.db.set_value("Shelving Log", kept_log, "stem_qty", total, update_modified=False)
		frappe.db.set_value("Shelf", shelf, "modified", frappe.utils.now(), update_modified=False)

	if not dry_run:
		frappe.db.commit()
	print(f"{'DRY RUN: ' if dry_run else ''}{merged} duplicate groups, {stems} stems")
	return {"groups": merged, "stems": stems, "dry_run": dry_run}
