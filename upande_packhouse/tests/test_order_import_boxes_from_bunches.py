import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.order_import import _boxes_from_bunches


class UnitTestBoxesFromBunches(UnitTestCase):
	def test_exact_division_succeeds(self):
		bi = frappe._dict({"bunches_per_box": 10})
		boxes, error = _boxes_from_bunches(bi, 30)
		self.assertEqual(boxes, 3)
		self.assertIsNone(error)

	def test_uneven_division_is_flagged_not_rounded(self):
		bi = frappe._dict({"bunches_per_box": 10})
		boxes, error = _boxes_from_bunches(bi, 25)
		self.assertIsNone(boxes)
		self.assertIn("doesn't divide evenly", error)

	def test_zero_bunches_per_box_is_flagged(self):
		bi = frappe._dict({"bunches_per_box": 0})
		boxes, error = _boxes_from_bunches(bi, 20)
		self.assertIsNone(boxes)
		self.assertIn("Bunches Per Box", error)

	def test_zero_bunches_ordered_is_flagged_not_zero_boxes(self):
		bi = frappe._dict({"bunches_per_box": 10})
		boxes, error = _boxes_from_bunches(bi, 0)
		self.assertIsNone(boxes)
		self.assertIsNotNone(error)
