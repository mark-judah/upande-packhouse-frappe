"""Release errors seen on 2026-11-25 (Kapkolia, delivery 27-11).

- A pick row left on a live pick list after its allocation was cancelled still holds its stems: its release had
  moved them to Graded Sold, so offering them again made the next release fail "N needed in <store>, 0.0 on hand".
- Two proposals saved for one order (a save racing another): the release sent the first and reported the copy as
  "No complete box yet" -- one proposal per order is kept.
"""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da
from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import sales_allocation_v2 as sa


def _row(bucket, item, length, total, allocated):
	return frappe._dict(
		bucket_id=bucket,
		item_code=item,
		stem_length=length,
		total_qty=total,
		allocated_qty=allocated,
		available_qty=max(0, total - allocated),
	)


class UnitTestOrphanPicksHold(UnitTestCase):
	def test_an_unissued_pick_row_without_allocation_holds_its_stems(self):
		rows = [
			_row("GI28AO", "Bellalinda Cerise", "72cm", 40, 0),
			_row("87JOH3", "Bellalinda Cerise", "72cm", 100, 0),
		]
		with patch.object(
			sa.frappe.db, "sql", return_value=[("gi28ao", "Bellalinda Cerise", "72cm", 40)]
		) as q:
			sa._hold_orphan_picks(rows)
		self.assertEqual(set(q.call_args[0][1]["buckets"]), {"GI28AO", "87JOH3"})
		self.assertEqual((rows[0].available_qty, rows[0].allocated_qty), (0, 40))
		self.assertEqual(rows[1].available_qty, 100)

	def test_another_length_is_not_held(self):
		rows = [_row("GI28AO", "Bellalinda Cerise", "62cm", 40, 0)]
		with patch.object(sa.frappe.db, "sql", return_value=[("GI28AO", "Bellalinda Cerise", "72cm", 40)]):
			sa._hold_orphan_picks(rows)
		self.assertEqual(rows[0].available_qty, 40)

	def test_no_rows_reads_nothing(self):
		with patch.object(sa.frappe.db, "sql") as q:
			sa._hold_orphan_picks([])
		q.assert_not_called()


class UnitTestOneProposalPerOrder(UnitTestCase):
	def tearDown(self):
		da._use_group(None, {})

	def test_the_latest_proposal_of_an_order_is_the_one_released(self):
		da._use_group(None, {})
		listed = [
			frappe._dict(name="PA-1", sales_order="SO-A"),
			frappe._dict(name="PA-2", sales_order="SO-A"),
			frappe._dict(name="PA-3", sales_order="SO-B"),
		]
		docs = {
			n: frappe._dict(
				name=n,
				sales_order=so,
				customer="C",
				delivery_date="2026-11-27",
				source="Picked",
				boxes=1,
				stems=10,
				allocations=[],
			)
			for n, so in (("PA-1", "SO-A"), ("PA-2", "SO-A"), ("PA-3", "SO-B"))
		}
		with (
			patch.object(da.frappe, "get_all", return_value=listed),
			patch.object(da.frappe, "get_doc", side_effect=lambda _dt, n: docs[n]),
			patch.object(da.frappe.db, "get_value", return_value=""),
		):
			p = da._proposal("Kapkolia", "2026-11-27", "2026-11-27")
		self.assertEqual([o["name"] for o in p["orders"]], ["PA-2", "PA-3"])
