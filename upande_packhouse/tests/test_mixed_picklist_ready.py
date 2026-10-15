from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.server_scripts import create_mixed_box_picklist as box
from upande_packhouse.server_scripts import create_mixed_bunch_picklist as bunch

# A mixed order's pick rows for a SALES-SHELF bucket (e.g. Karen's own stock) are already
# where they will be issued: awaiting_transfer = 0 and custom_ready_for_packing = 1, the same
# as the straight-box rows and sales_allocation.py. Left at 0, the issue scan reads the row
# as "waiting for transfer from <its farm>" whenever the transfer hub is another sales farm,
# and the order can be neither listed nor issued. A REMOTE bucket stays awaiting_transfer = 1
# and not ready until it is shelved at the hub.

SO_ITEM = frappe._dict({"item_name": "Bombastic", "custom_length": "52cm"})
ALLOC = frappe._dict({"stem_length": "52cm"})


def _sales(bucket, qty=50):
	return {"bucket_id": bucket, "qty": qty, "warehouse": "Karen Receiving Cold Store - KR",
		"_shelf_farm": "Karen", "_is_sales_shelf": 1}


def _remote(bucket, qty=50):
	return {"bucket_id": bucket, "qty": qty, "warehouse": "Chepsito Store - CH",
		"_shelf_farm": "Chepsito", "_is_sales_shelf": 0}


def _unflagged(bucket, qty=50):
	return {"bucket_id": bucket, "qty": qty, "warehouse": "Karen Receiving Cold Store - KR"}


def _flags(rows):
	return {r["bucket"]: (r["awaiting_transfer"], r["custom_ready_for_packing"]) for r in rows}


class UnitTestMixedBoxReady(UnitTestCase):
	def _flat(self, allocs):
		with patch.object(box, "_lookup_shelf", return_value="K58M"):
			return box._generate_flat_locations(
				ALLOC, SO_ITEM, "SO-1", "soi-1", allocs, 1, "Stem", "Stem", "Karen", "Bombastic")

	def _split(self, allocs, slots):
		with patch.object(box, "_lookup_shelf", return_value="K58M"):
			return box._generate_box_locations_with_splitting(
				ALLOC, SO_ITEM, "SO-1", "soi-1", allocs, 50, slots, 1, "Stem", "Stem", "Karen", "Bombastic")

	def test_flat_rows(self):
		rows = self._flat([_sales("S1"), _remote("R1")])
		self.assertEqual(_flags(rows), {"S1": (0, 1), "R1": (1, 0)})

	def test_split_rows(self):
		rows = self._split([_sales("S1"), _remote("R1")], [(1, 50), (2, 50)])
		self.assertEqual(_flags(rows), {"S1": (0, 1), "R1": (1, 0)})

	def test_a_bucket_that_does_not_say_is_a_sales_shelf_bucket(self):
		self.assertEqual(_flags(self._flat([_unflagged("U1")])), {"U1": (0, 1)})
		self.assertEqual(_flags(self._split([_unflagged("U1")], [(1, 50)])), {"U1": (0, 1)})

	def test_a_bucket_is_never_both_awaiting_and_ready(self):
		rows = self._flat([_sales("S1"), _remote("R1"), _unflagged("U1")])
		for r in rows:
			self.assertNotEqual(r["awaiting_transfer"], r["custom_ready_for_packing"], r["bucket"])


class UnitTestMixedBunchReady(UnitTestCase):
	def _row(self, awaiting):
		return bunch._bunch_row(SO_ITEM, "Bombastic", "SO-1", "soi-1", ALLOC, "B1", 50,
			"Karen Receiving Cold Store - KR", "", 0, awaiting, "K58M", "Karen", 1, "Stem", "Stem", 1)

	def test_row_not_awaiting_transfer_is_ready(self):
		row = self._row(0)
		self.assertEqual((row["awaiting_transfer"], row["custom_ready_for_packing"]), (0, 1))

	def test_row_awaiting_transfer_is_not_ready(self):
		row = self._row(1)
		self.assertEqual((row["awaiting_transfer"], row["custom_ready_for_packing"]), (1, 0))

	def test_flat_rows(self):
		with patch.object(bunch, "_lookup_shelf", return_value="K58M"):
			rows = bunch._generate_bunch_flat_locations(
				ALLOC, SO_ITEM, "SO-1", "soi-1", [_sales("S1"), _remote("R1")], 1, "Stem", "Stem", "Karen", "Bombastic")
		self.assertEqual(_flags(rows), {"S1": (0, 1), "R1": (1, 0)})

	def test_split_rows(self):
		with patch.object(bunch, "_lookup_shelf", return_value="K58M"):
			rows = bunch._generate_bunch_locations_with_splitting(
				ALLOC, SO_ITEM, "SO-1", "soi-1", [_sales("S1"), _remote("R1")], 50, [(1, 50), (2, 50)],
				1, "Stem", "Stem", "Karen", "Bombastic")
		self.assertEqual(_flags(rows), {"S1": (0, 1), "R1": (1, 0)})
