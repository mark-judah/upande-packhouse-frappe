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

	def _make_bunch_aware_spec(
		self, name, bunch_id="B1", bunches_per_box=10, stems_per_bunch=5, pack_rate=50
	):
		"""A genuinely bunch_aware Specification: its one Approved Variety row
		carries a non-blank bunch_id, paired with a Box Item row sharing that
		same bunch_id (one Box Item per colour - see
		spec_autofill._bunch_shape), so _bunch_shape(doc) returns
		bunch_aware=True instead of falling back to the flat picker the way
		every _make_spec fixture above does (those never set bunch_id)."""
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
				"box_assortment": "Mono Box",
				"box_type": BOX_TYPE,
				"approved_varieties": [
					{
						"colour": "Test Wide Matrix Red",
						"variety": "Test Wide Matrix Variety Red",
						"is_primary": 1,
						"bunch_id": bunch_id,
					}
				],
				"box_items": [
					{
						"bunch_id": bunch_id,
						"bunch_type": "Mono Bunch",
						"stems_per_bunch": stems_per_bunch,
						"length": STEM_LENGTH,
						"bunches_per_box": bunches_per_box,
						"pack_rate": pack_rate,
					}
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

	def test_mix_spec_bunch_rate_matches_sum_across_colours_no_warning(self):
		# The file's Bunch Rate on a real multi-colour mix line is the TOTAL
		# stems across the whole bouquet (sum across every colour), not any
		# one colour's own stems_per_bunch - comparing against only the
		# first colour (5) would false-positive here, since the file value
		# (10) actually matches the SUM of both colours (5 + 5).
		self._make_spec(
			"Test Wide Matrix Mix Spec Bunch Rate Sum Match",
			[
				("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True),
				("Test Wide Matrix Blue", "Test Wide Matrix Variety Blue", True),
			],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Mix Spec Bunch Rate Sum Match",
			"_bunches_ordered": 30,
			"_bunch_rate": 10,  # 5 (red) + 5 (blue) = 10
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertEqual(len(rows), 2)
		for r in rows:
			self.assertNotIn("_bunch_rate_mismatch", r)

	def test_mix_spec_bunch_rate_mismatch_against_sum_is_flagged(self):
		self._make_spec(
			"Test Wide Matrix Mix Spec Bunch Rate Sum Mismatch",
			[
				("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True),
				("Test Wide Matrix Blue", "Test Wide Matrix Variety Blue", True),
			],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Mix Spec Bunch Rate Sum Mismatch",
			"_bunches_ordered": 30,
			"_bunch_rate": 7,  # doesn't match the sum (10)
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertIn("_bunch_rate_mismatch", rows[0])
		self.assertIn("10", rows[0]["_bunch_rate_mismatch"])

	def test_unparseable_bunch_rate_skips_cross_check_silently(self):
		# A file Bunch Rate value that isn't a number at all must never
		# crash the row (a bare int() would raise) nor produce a bogus
		# warning - it's skipped silently.
		self._make_spec(
			"Test Wide Matrix Spec Bunch Rate Garbage",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Spec Bunch Rate Garbage",
			"_bunches_ordered": 20,
			"_bunch_rate": "n/a",
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

	def test_bunch_aware_spec_with_bunches_ordered_still_needs_boxes(self):
		# The re-added guard inside `if bunch_aware:` only fires once
		# _resolve_spec_row actually reaches the bunch_aware branch, which
		# needs a genuinely bunch_aware spec (every Approved Variety row
		# carrying a non-blank bunch_id - see spec_autofill._bunch_shape).
		# test_bunch_aware_spec_without_boxes_still_needs_boxes above uses a
		# flat spec with NO _bunches_ordered at all, so it's caught by the
		# earlier top-level guard before bunch_aware is even checked; this
		# test supplies _bunches_ordered on a real bunch-aware spec to prove
		# bunches-input is correctly NOT honoured there (bunches-input is
		# flat-spec only, per this plan's Global Constraints).
		self._make_bunch_aware_spec("Test Wide Matrix Bunch Aware Spec")
		item_row = {
			"custom_line": "Test Wide Matrix Bunch Aware Spec",
			"_bunches_ordered": 20,
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(rows)
		self.assertEqual(error, "needs Number of Boxes set on this row")

	def test_boxes_given_directly_is_unaffected_by_new_bunches_path(self):
		# Pre-existing behaviour (today's only path, used by every existing
		# Standard Rows platform): custom_number_of_boxes given directly, no
		# _bunches_ordered/_bunch_rate at all. Must produce output identical
		# to before this task - in particular, no _bunch_rate_mismatch key
		# should appear on a row that never supplied a Bunch Rate to compare
		# against.
		self._make_spec(
			"Test Wide Matrix Spec Direct Boxes",
			[("Test Wide Matrix Red", "Test Wide Matrix Variety Red", True)],
			bunches_per_box=10,
			stems_per_bunch=5,
		)
		item_row = {
			"custom_line": "Test Wide Matrix Spec Direct Boxes",
			"custom_number_of_boxes": 4,
		}
		rows, error = _resolve_spec_row(item_row, "Test Customer", {"mix_group": 1, "bunch_group": 1})
		self.assertIsNone(error)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["item_code"], "Test Wide Matrix Variety Red")
		self.assertEqual(rows[0]["custom_number_of_boxes"], 4)
		self.assertNotIn("_bunch_rate_mismatch", rows[0])
