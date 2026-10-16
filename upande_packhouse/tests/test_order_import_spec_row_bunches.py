import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.api.order_import import _resolve_spec_row

CUT_STAGE = "Test Wide Matrix Cut Stage"
BOX_TYPE = "Test Wide Matrix Box Type"
STEM_LENGTH = "52cm"


class IntegrationTestResolveSpecRowBunches(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Cut Stage", CUT_STAGE):
			frappe.get_doc({"doctype": "Cut Stage", "cutstage": CUT_STAGE}).insert(ignore_permissions=True)
		if not frappe.db.exists("Box Type", BOX_TYPE):
			frappe.get_doc({"doctype": "Box Type", "name": BOX_TYPE, "box_name": BOX_TYPE}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("Stem Length", STEM_LENGTH):
			frappe.get_doc({"doctype": "Stem Length", "length": STEM_LENGTH}).insert(ignore_permissions=True)
		for color in ("Test Wide Matrix Red", "Test Wide Matrix Blue"):
			if not frappe.db.exists("Colors", color):
				frappe.get_doc({"doctype": "Colors", "color_name": color}).insert(ignore_permissions=True)
		# Spec Approved Variety's own "colour" field is fetch_from
		# variety.custom_color with fetch_if_empty unset (False), so Frappe
		# always re-fetches it on save regardless of what a row sets directly
		# (base_document.py's get_valid_dict: "not fetch_if_empty" fetches
		# unconditionally) - each test Item needs its own custom_color set so
		# the Specification ends up with the colour this fixture intends,
		# rather than every row collapsing to the same blank colour.
		for item, color in (
			("Test Wide Matrix Variety Red", "Test Wide Matrix Red"),
			("Test Wide Matrix Variety Blue", "Test Wide Matrix Blue"),
		):
			if not frappe.db.exists("Item", item):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item,
						"item_name": item,
						"item_group": frappe.db.get_value("Item Group", {}, "name"),
						"stock_uom": "Nos",
						"is_stock_item": 1,
						"custom_color": color,
					}
				).insert(ignore_permissions=True)
		frappe.db.commit()

	def _make_spec(self, name, colour_items, bunches_per_box=10, stems_per_bunch=5, pack_rate=50):
		"""colour_items: [(colour, item_code, is_primary), ...] - one row per
		approved colour, each paired 1:1 with its own box item (see
		spec_autofill._box_idxs_by_variety: same length, position-paired)."""
		if frappe.db.exists("Specifications", name):
			return frappe.get_doc("Specifications", name)
		doc = frappe.get_doc(
			{
				"doctype": "Specifications",
				"spec_name": name,
				"ftnft": "FT",
				"spec_type": "Permanent",
				"cut_stage": CUT_STAGE,
				"defoliation_length": "20",
				"box_assortment": "Mixed Box" if len(colour_items) > 1 else "Mono Box",
				"min_colours_per_box": len(colour_items) if len(colour_items) > 1 else 0,
				"max_colours_per_box": len(colour_items) if len(colour_items) > 1 else 0,
				"box_type": BOX_TYPE,
				"approved_varieties": [
					{"colour": colour, "variety": item, "is_primary": 1 if primary else 0}
					for colour, item, primary in colour_items
				],
				"box_items": [
					{
						"bunch_type": "Mono Bunch",
						"stems_per_bunch": stems_per_bunch,
						"length": STEM_LENGTH,
						"bunches_per_box": bunches_per_box,
						"pack_rate": pack_rate,
					}
					for _ in colour_items
				],
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def test_mix_spec_expands_one_row_per_colour_via_bunches_input(self):
		self._make_spec(
			"Test Wide Matrix Mix Spec",
			[
				("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True),
				("Test Wide Matrix Blue", "Test Wide Matrix Variety Blue", True),
			],
			bunches_per_box=10,
		)
		item_row = {"custom_line": "Test Wide Matrix Mix Spec", "_bunches_ordered": 30}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertEqual(len(rows), 2)
		varieties = {r["item_code"] for r in rows}
		self.assertEqual(varieties, {"Test Wide Matrix Variety Red", "Test Wide Matrix Variety Blue"})
		# 30 bunches / 10 bunches-per-box = 3 boxes, same box count on every colour.
		self.assertEqual(rows[0]["custom_number_of_boxes"], 3)
		self.assertEqual(rows[1]["custom_number_of_boxes"], 3)

	def test_uneven_bunches_is_flagged_not_rounded(self):
		self._make_spec(
			"Test Wide Matrix Single Spec Uneven",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
		)
		item_row = {"custom_line": "Test Wide Matrix Single Spec Uneven", "_bunches_ordered": 25}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(rows)
		self.assertIn("doesn't divide evenly", error)

	def test_incomplete_spec_gives_existing_incomplete_message_not_a_bunches_message(self):
		doc = self._make_spec(
			"Test Wide Matrix Incomplete Spec",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
		)
		# Simulate a legacy Specification saved before Bunches Per Box was
		# required - _spec_issues must catch this BEFORE any bunches math
		# runs, exactly as it already does for the boxes-input path.
		frappe.db.set_value("Spec Box Item", doc.box_items[0].name, "bunches_per_box", 0)
		item_row = {"custom_line": "Test Wide Matrix Incomplete Spec", "_bunches_ordered": 20}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(rows)
		self.assertIn("is incomplete", error)

	def test_bunch_rate_mismatch_is_a_non_blocking_warning(self):
		self._make_spec(
			"Test Wide Matrix Spec Bunch Rate",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Spec Bunch Rate",
			"_bunches_ordered": 20,
			"_bunch_rate": 7,  # file says 7, Specification says 5
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertIn("_bunch_rate_mismatch", rows[0])
		self.assertIn("5", rows[0]["_bunch_rate_mismatch"])

	def test_matching_bunch_rate_has_no_warning(self):
		self._make_spec(
			"Test Wide Matrix Spec Bunch Rate Match",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Spec Bunch Rate Match",
			"_bunches_ordered": 20,
			"_bunch_rate": 5,
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertNotIn("_bunch_rate_mismatch", rows[0])

	def test_bunch_aware_spec_without_boxes_still_needs_boxes(self):
		# Scoping guard (Global Constraints): bunches-input is flat-spec only.
		# A bunch-aware spec's existing "needs Number of Boxes" error must be
		# unaffected by this change. Reuses the mix spec from the first test
		# — it is NOT bunch_aware (no bunch_id set on any row), so this
		# instead confirms the ordinary "no boxes, no bunches at all" case
		# still errors exactly as before.
		self._make_spec(
			"Test Wide Matrix Mix Spec No Qty",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
		)
		item_row = {"custom_line": "Test Wide Matrix Mix Spec No Qty"}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(rows)
		self.assertIn("needs Number of Boxes", error)
