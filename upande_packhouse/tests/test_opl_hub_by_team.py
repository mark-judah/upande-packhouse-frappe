from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.remote_transfer import transfer_scheduling as tc

# Where a pick list's remote buckets go: the sales farm of the team packing it first, then the order's farm.
# An order split between teams of two sales farms sends each pick list to its own team's. Database mocked.


class UnitTestOplHubByTeam(UnitTestCase):
	def _hubs(self, rows):
		with (
			patch.object(tc.frappe.db, "sql", return_value=[frappe._dict(r) for r in rows]),
			patch.object(tc, "_hub_farms", return_value={"kapkolia": "Kapkolia", "chepsito": "Chepsito"}),
			patch.object(tc, "transfer_hub", return_value="Kapkolia"),
			patch.object(tc, "_so_farm_expr", return_value="NULL"),
		):
			return tc.opl_hubs([r["name"] for r in rows])

	def test_the_team_s_sales_farm_wins_over_the_order_s(self):
		out = self._hubs(
			[
				{
					"name": "OPL-C",
					"sales_order": "SO-1",
					"farm": "Kapkolia",
					"team_farm": "Chepsito",
					"so_farm": "Kapkolia",
				},
				{
					"name": "OPL-K",
					"sales_order": "SO-1",
					"farm": "Kapkolia",
					"team_farm": "Kapkolia",
					"so_farm": "Kapkolia",
				},
			]
		)
		self.assertEqual(out, {"OPL-C": "Chepsito", "OPL-K": "Kapkolia"})

	def test_no_team_falls_back_to_the_order_s_farm(self):
		out = self._hubs(
			[{"name": "OPL-1", "sales_order": "SO-1", "farm": None, "team_farm": None, "so_farm": "Chepsito"}]
		)
		self.assertEqual(out, {"OPL-1": "Chepsito"})
