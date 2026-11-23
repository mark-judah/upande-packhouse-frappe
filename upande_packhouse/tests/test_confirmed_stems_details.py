from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

from upande_packhouse import allocation_plan as plans

# The Sales Order's Confirmed Stems rows show, beside the stems, the line's variety, the team it is confirmed for
# and the sales farm that packs it (that team's). The database is mocked.


class UnitTestConfirmedStemsDetails(UnitTestCase):
	def _details(self, team, has=("item_code", "team", "processing_sales_farm")):
		meta = MagicMock()
		meta.has_field = lambda f: f in has
		with (
			patch.object(plans.frappe, "get_meta", return_value=meta),
			patch.object(plans, "team_for_line", return_value=team),
			patch.object(plans.frappe.db, "get_value", return_value="Salinero"),
			patch(
				"upande_packhouse.api.v2.allocation_teams.team_sales_farms",
				return_value={"Bravo": "Karen", "Eldama A": "Kapkolia"},
			),
		):
			return plans._confirmed_row_details("SOI-1")

	def test_a_row_shows_its_variety_team_and_the_teams_sales_farm(self):
		self.assertEqual(
			self._details("Bravo"),
			{"item_code": "Salinero", "team": "Bravo", "processing_sales_farm": "Karen"},
		)

	def test_a_line_with_no_team_shows_no_sales_farm(self):
		self.assertEqual(self._details(None)["processing_sales_farm"], None)

	def test_only_the_fields_the_table_has_are_written(self):
		self.assertEqual(self._details("Bravo", has=("team",)), {"team": "Bravo"})
