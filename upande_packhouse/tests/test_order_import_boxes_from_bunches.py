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

	def test_comma_formatted_bunches_ordered_parses_correctly(self):
		# A real spreadsheet export commonly formats a large bunch count
		# with a thousands-separator comma - must parse via flt (which
		# strips it), not crash or silently become 0 like a bare int()
		# would on "1,200".
		bi = frappe._dict({"bunches_per_box": 10})
		boxes, error = _boxes_from_bunches(bi, "1,200")
		self.assertEqual(boxes, 120)
		self.assertIsNone(error)

	def test_non_whole_number_bunches_ordered_is_a_clean_error_not_a_crash(self):
		# A non-integer-looking string ("14.5") must never reach a bare
		# int(), which would raise and crash the entire order's preview -
		# and must never be truncated/rounded silently either (the plan's
		# "never auto-round" rule) - it's a per-row error instead.
		bi = frappe._dict({"bunches_per_box": 10})
		boxes, error = _boxes_from_bunches(bi, "14.5")
		self.assertIsNone(boxes)
		self.assertIn("not a whole number", error)
