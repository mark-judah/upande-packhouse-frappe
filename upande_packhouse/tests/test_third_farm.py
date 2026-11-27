"""A bucket on a truck shelved at a third farm waits there for a trip on (2026-11-22: SANDY-05285 at Kapkolia).

Run:  bench --site kaitet.local run-tests --app upande_packhouse --module upande_packhouse.tests.test_third_farm
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from upande_packhouse.api.remote_transfer import third_farm as tf


def _row(name, parent, row_farm):
	return frappe._dict(name=name, parent=parent, row_farm=row_farm)


class TestWaitAtThirdFarm(unittest.TestCase):
	def _run(self, rows, hubs, farm="Kapkolia", trips=None):
		set_rows, dropped = [], []
		with (
			patch.object(tf.frappe.db, "sql", return_value=rows),
			patch.object(tf.frappe.db, "has_column", return_value=True),
			patch.object(
				tf.frappe.db, "set_value", side_effect=lambda dt, n, v: set_rows.append((n, dict(v)))
			),
			patch.object(tf.ts, "opl_hubs", return_value=hubs),
			patch.object(tf.ts, "_bucket_trip_rows", return_value=trips or {}),
			patch.object(tf.ts, "_drop_bucket_from_trips", side_effect=lambda b, w, n: dropped.append(b)),
			patch.object(tf.ts, "log_transfer_event"),
			patch.object(tf.frappe, "get_doc", return_value=MagicMock()),
		):
			out = tf.wait_at_third_farm("abc123", farm, "KAP-S1")
		return out, set_rows, dropped

	def test_dropped_on_the_way_waits_at_that_farm(self):
		trips = {"ABC123": [frappe._dict(trip="BRT-1", opl="OPL-1")]}
		out, set_rows, dropped = self._run(
			[_row("R1", "OPL-1", "Simotwo")], {"OPL-1": "Chepsito"}, trips=trips
		)
		self.assertEqual(out, ["OPL-1"])
		name, values = set_rows[0]
		self.assertEqual(name, "R1")
		self.assertEqual(values["farm"], "Kapkolia")
		self.assertEqual(values["shelf"], "KAP-S1")
		self.assertEqual((values["in_transit"], values["awaiting_transfer"]), (0, 1))
		self.assertIsNone(values["transit_truck"])
		self.assertEqual(dropped, ["ABC123"])

	def test_shelved_at_its_sales_farm_is_its_arrival(self):
		out, set_rows, _d = self._run([_row("R1", "OPL-1", "Simotwo")], {"OPL-1": "Kapkolia"})
		self.assertEqual((out, set_rows), ([], []))

	def test_shelved_back_at_the_farm_it_left_is_left_alone(self):
		out, set_rows, _d = self._run([_row("R1", "OPL-1", "Kapkolia")], {"OPL-1": "Chepsito"})
		self.assertEqual((out, set_rows), ([], []))

	def test_not_in_transit(self):
		out, set_rows, _d = self._run([], {})
		self.assertEqual((out, set_rows), ([], []))


def _moving(parent, farm, src, transit=1, truck="KDP 283L"):
	return frappe._dict(
		parent=parent,
		farm=farm,
		source_warehouse="{0} Receiving Cold Store - KR".format(src),
		transit=transit,
		truck=truck,
	)


class TestThirdFarmShelvingBlock(unittest.TestCase):
	def _run(self, rows, hubs, farm):
		with (
			patch.object(tf.frappe.db, "sql", return_value=rows),
			patch.object(tf.ts, "opl_hubs", return_value=hubs),
		):
			return tf.third_farm_shelving_block("0H21WD", farm)

	def test_a_third_farm_is_refused(self):
		msg = self._run([_moving("OPL-1", "Kaptumbo", "Kaptumbo")], {"OPL-1": "Kapkolia"}, "Chepsito")
		self.assertIn("can't be shelved at Chepsito", msg)
		self.assertIn("Shelve it at Kapkolia, or back at Kaptumbo", msg)
		self.assertIn("on KDP 283L", msg)

	def test_its_sales_farm_is_its_arrival(self):
		self.assertIsNone(
			self._run([_moving("OPL-1", "Kaptumbo", "Kaptumbo")], {"OPL-1": "Kapkolia"}, "Kapkolia")
		)

	def test_back_at_the_farm_it_left(self):
		self.assertIsNone(
			self._run([_moving("OPL-1", "Kaptumbo", "Kaptumbo")], {"OPL-1": "Kapkolia"}, "kaptumbo")
		)

	def test_on_a_trolley_at_another_farm_is_refused(self):
		msg = self._run([_moving("OPL-1", "Simotwo", "Simotwo", 0, None)], {"OPL-1": "Kapkolia"}, "Chepsito")
		self.assertIn("on a trolley", msg)

	def test_any_of_its_orders_may_take_it(self):
		rows = [_moving("OPL-1", "Kaptumbo", "Kaptumbo"), _moving("OPL-2", "Kaptumbo", "Kaptumbo")]
		self.assertIsNone(self._run(rows, {"OPL-1": "Kapkolia", "OPL-2": "Chepsito"}, "Chepsito"))

	def test_not_moving(self):
		self.assertIsNone(self._run([], {}, "Chepsito"))
