"""One-off repair of remote-transfer stock, run once by `bench migrate`.

Until the transfer fixes, a remote farm's bucket could move in the ledger before it
physically left the farm (shelving at the farm posted the Remote Transfers leg and
stamped the shelf row with Kapkolia Receiving), Offline Issuing took stems from that
wrong store (sometimes twice), and Shelf Items stayed behind after a bucket went on the
truck or was issued. In order:

  1. clear Shelf Items of buckets that already left the shelf;
  2. point open pick rows back to the bucket's own farm cold store;
  3. square each bucket's stems between its farm store and Kapkolia Receiving
     (cancel duplicate Offline Issuing, move the overlap);
  4. return stems that moved before the bucket left its farm (reversing any sale
     posted for its open orders) — Kapkolia shelving posts them at the right time.

Each step commits as it goes and is safe to re-run; a failing step is logged and the
others still run. The full result is saved in the Error Log ("Remote transfer stock
repair") for review.
"""

import frappe


def execute():
	from upande_packhouse.api import transfer_control as tc

	steps = [
		("clear stale shelf items", lambda: tc.clear_stale_shelf_items(dry_run=0)),
		("pick rows to own farm", lambda: tc.repair_remote_source_warehouse(dry_run=0)),
		("square bucket stock", lambda: tc.fix_bucket_stock_location(dry_run=0)),
		("return early arrivals", lambda: tc.return_early_arrivals(dry_run=0)),
	]
	report = {}
	for label, run in steps:
		try:
			result = run()
			report[label] = result
			summary = {k: v for k, v in result.items() if not isinstance(v, list)}
			print(f"Remote transfer stock repair — {label}: {summary}")
		except Exception:
			frappe.db.rollback()
			report[label] = {"error": frappe.get_traceback()}
			print(f"Remote transfer stock repair — {label}: FAILED (see Error Log)")
	frappe.log_error(
		title="Remote transfer stock repair",
		message=frappe.as_json(report, indent=1),
	)
	frappe.db.commit()  # nosemgrep: frappe-manual-commit
