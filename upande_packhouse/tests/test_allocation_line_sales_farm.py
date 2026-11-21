from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

# An order's lines can be planned to teams of different sales farms: each line is allocated at its team's sales farm
# (else the order's own). Saving at a sales farm refuses only lines of another one. The database is mocked.

LINES = {"L-KAP": "Kapkolia", "L-KAR": "Karen"}


def _check(location, lines=None):
	allocations = [{"sales_order_item": n, "item_code": n} for n in lines] if lines is not None else None
	with (
		patch.object(da, "_line_locations", return_value=dict(LINES)),
		patch(
			"upande_packhouse.api.v2.allocation_teams.sales_farm_for_location",
			side_effect=lambda loc: loc,
		),
	):
		da._check_location("SO-1", location, allocations)


class UnitTestAllocationLineSalesFarm(UnitTestCase):
	def test_each_sales_farm_saves_its_own_lines(self):
		_check("Kapkolia", ["L-KAP"])
		_check("Karen", ["L-KAR"])

	def test_a_line_of_another_sales_farm_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as e:
			_check("Kapkolia", ["L-KAP", "L-KAR"])
		self.assertIn("L-KAR is processed at Karen, not Kapkolia", str(e.exception))

	def test_a_split_order_belongs_to_both_sales_farms(self):
		_check("Kapkolia")
		_check("Karen")
		with self.assertRaises(frappe.ValidationError):
			_check("Chepsito")
