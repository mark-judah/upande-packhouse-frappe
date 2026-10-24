from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.mobile import api

# The Precooling screen lists the boxes waiting in the dispatch coldstore by drop-off
# point, with the spot each was scanned to and since when -- the caller's location only.
# The database is mocked: these run on a bare site (CI) as well.


def _box(name, dp, so, spot, since):
	return frappe._dict(
		name=name,
		box_number=1,
		customer="C",
		staging_location=spot,
		delivery_point=dp,
		sales_order=so,
		delivery_date="2026-10-25",
		since=since,
	)


class UnitTestColdstoreBoxes(UnitTestCase):
	ROWS = (
		_box("B1", "AIRFLO", "SO-R", "CLD5 B3", "2026-10-24 08:00:00"),
		_box("B2", "DHL", "SO-R", "CLD5 F5", "2026-10-24 09:00:00"),
		_box("B3", "AIRFLO", "SO-R", "CLD5 B3", "2026-10-24 10:00:00"),
		_box("B4", "MITCHEL COTTS", "SO-K", "DISPATCH-KAREN-A1", "2026-10-24 11:00:00"),
	)

	def _sql(self, query, *args, **kwargs):
		# Only the coldstore query is faked; anything else (meta loads) would cache junk.
		if "tabBox Label" in query and "staging_location" in query:
			return list(self.ROWS)
		return self._real_sql(query, *args, **kwargs)

	def _fetch(self, location):
		self._real_sql = frappe.db.sql
		with (
			patch.object(api.frappe.db, "sql", side_effect=self._sql),
			patch.object(
				api,
				"location_sales_orders",
				side_effect=lambda d, loc: {"Ravine": {"SO-R"}, "Karen": {"SO-K"}}[loc],
			),
			patch.object(api, "_station_location", return_value=None),
		):
			return api.fetchColdstoreBoxes(location)

	def test_grouped_by_drop_off_point_for_the_location(self):
		r = self._fetch("Ravine")
		self.assertEqual(r["total"], 3)
		self.assertEqual(
			[(g["delivery_point"], [b["box_label"] for b in g["boxes"]]) for g in r["delivery_points"]],
			[("AIRFLO", ["B1", "B3"]), ("DHL", ["B2"])],
		)
		self.assertEqual(r["delivery_points"][0]["boxes"][0]["staging_location"], "CLD5 B3")
		self.assertEqual(r["delivery_points"][0]["boxes"][0]["since"], "2026-10-24 08:00:00")

	def test_other_location_boxes_left_out(self):
		r = self._fetch("Karen")
		self.assertEqual([g["delivery_point"] for g in r["delivery_points"]], ["MITCHEL COTTS"])
