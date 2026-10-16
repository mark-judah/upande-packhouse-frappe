import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.api.order_import import WIDE_MATRIX_SYNTHETIC_HEADER, expand_wide_matrix


def _layout(**overrides):
	"""A valid Wide Matrix layout config matching the 2-group fixture below,
	with any field overridden for a specific test."""
	base = {
		"matrix_group_label_row": 2,
		"matrix_order_date_row": 3,
		"matrix_delivery_date_row": 4,
		"matrix_po_row": 5,
		"matrix_items_start_row": 6,
		"matrix_code_column": 0,
		"matrix_description_column": 1,
		"matrix_bunch_rate_column": 3,
		"matrix_first_group_column": 4,
		"matrix_group_column_width": 2,
		"matrix_stop_value": "Total",
	}
	base.update(overrides)
	return frappe._dict(base)


# Synthetic fixture modeled on the real Flamingo file's structure (title row,
# customer row, colour-group label row, order-date row, delivery-date row,
# PO-number+confirmation row, item rows, a Total row, trailing blank/legend
# rows) - values are invented, not real customer data. Two groups only
# (fewer than the reference file's 6), one row that's zero in every group
# (must be skipped entirely), one row present in only one group.
TWO_GROUP_ROWS = [
	["TESTCO01 PO lines by date", "GBP", "01/01/26 - 07/01/26"],  # 0: title
	["TESTCUST"],  # 1: customer name
	["", "", "", "", "Alpha", "", "Beta"],  # 2: group labels
	["", "", "", "", "01/01/2026", "", "02/01/2026"],  # 3: order dates
	["", "", "", "", "03/01/2026", "", "04/01/2026"],  # 4: delivery dates
	["", "", "", "", "PO-1001", "confirmation", "PO-1002", "confirmation"],  # 5: PO numbers
	["ZEROCODE", "Zero Everywhere Spec", "1.0000", "20", "0", "0", "0", "0"],  # 6: all-zero row
	["MIXCODE", "Test Mix Spec x20 STEMS", "1.0000", "20", "40", "40", "0", "0"],  # 7: only Alpha
	["SINGLECODE", "Test Single Spec x10 STEMS", "0.5000", "10", "0", "0", "30", "30"],  # 8: only Beta
	[""],  # 9: blank separator
	["Total", "", "", "", "40", "", "30"],  # 10: Total row - stop marker
	[""],
	["LINE CODE", "LINE", "RATE", "BUNCH RATE", "", "BUNCHES"],  # legend, never reached
]


class UnitTestExpandWideMatrix(UnitTestCase):
	def test_pivots_two_groups_and_skips_all_zero_row(self):
		# header/rows split the same way the real client always does (see
		# applyParsedGrid in sales-order-v2.html/sales-order.html): the
		# file's actual row 0 is stripped off into `header`, never left in
		# `rows` - every matrix_*_row config value here is 0-indexed against
		# the FULL grid including that stripped row, so TWO_GROUP_ROWS[0]
		# (not an empty header) must be what's passed as `header`.
		header, rows = expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], _layout())
		self.assertEqual(header, WIDE_MATRIX_SYNTHETIC_HEADER)
		# ZEROCODE is dropped entirely (zero in every group); MIXCODE only
		# appears for Alpha, SINGLECODE only for Beta.
		self.assertEqual(
			rows,
			[
				["Test Mix Spec x20 STEMS", 40.0, "20", "PO-1001", "01/01/2026", "03/01/2026"],
				["Test Single Spec x10 STEMS", 30.0, "10", "PO-1002", "02/01/2026", "04/01/2026"],
			],
		)

	def test_stops_at_total_row_and_never_reads_the_legend(self):
		# The legend row below "Total" literally repeats "LINE CODE"/"LINE" as
		# plain text in the code/description columns - if the stop condition
		# didn't fire, it would be mis-read as a bogus extra item line.
		_header, rows = expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], _layout())
		descriptions = [r[0] for r in rows]
		self.assertNotIn("LINE", descriptions)

	def test_detects_fewer_groups_than_a_six_group_reference_file(self):
		# Only 2 groups configured/present here (vs. the real Flamingo file's
		# 6) - group count must come from the label row, not be hardcoded.
		_header, rows = expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], _layout())
		po_numbers = {r[3] for r in rows}
		self.assertEqual(po_numbers, {"PO-1001", "PO-1002"})

	def test_detects_more_groups_than_two(self):
		four_group_rows = [
			["TESTCO01 PO lines by date"],
			["TESTCUST"],
			["", "", "", "", "A", "", "B", "", "C", "", "D"],
			["", "", "", "", "01/01/2026", "", "02/01/2026", "", "03/01/2026", "", "04/01/2026"],
			["", "", "", "", "05/01/2026", "", "06/01/2026", "", "07/01/2026", "", "08/01/2026"],
			["", "", "", "", "PO-A", "x", "PO-B", "x", "PO-C", "x", "PO-D", "x"],
			["CODE1", "Four Group Spec x10 STEMS", "1.0", "10", "5", "5", "5", "5", "5", "5", "5", "5"],
			["Total"],
		]
		_header, rows = expand_wide_matrix(four_group_rows[0], four_group_rows[1:], _layout())
		self.assertEqual({r[3] for r in rows}, {"PO-A", "PO-B", "PO-C", "PO-D"})

	def test_rejects_unset_row_layout(self):
		# Every matrix_*_row field left at its doctype default of 0 - must
		# raise a clear configuration error, never silently parse row 0 as
		# every section.
		bad_layout = _layout(
			matrix_group_label_row=0,
			matrix_order_date_row=0,
			matrix_delivery_date_row=0,
			matrix_po_row=0,
			matrix_items_start_row=0,
		)
		with self.assertRaises(frappe.exceptions.ValidationError):
			expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], bad_layout)

	def test_rejects_colliding_columns(self):
		bad_layout = _layout(matrix_description_column=0)  # same as matrix_code_column
		with self.assertRaises(frappe.exceptions.ValidationError):
			expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], bad_layout)

	def test_rejects_group_column_width_below_one(self):
		bad_layout = _layout(matrix_group_column_width=0)
		with self.assertRaises(frappe.exceptions.ValidationError):
			expand_wide_matrix(TWO_GROUP_ROWS[0], TWO_GROUP_ROWS[1:], bad_layout)

	def test_pivot_matches_real_client_header_rows_split(self):
		# Pins the real calling convention directly (see applyParsedGrid in
		# upande_packhouse/www/sales-order-v2.html): given a whole raw grid,
		# the real client always does `header = grid[0]; rows = grid[1:]`
		# before ever calling buildPreview -> expand_wide_matrix. This test
		# builds the grid fresh and performs exactly that split itself,
		# rather than relying on a fixture already pre-split for it, so a
		# future regression in how `header`/`rows` are stitched back
		# together inside expand_wide_matrix is caught here directly.
		grid = TWO_GROUP_ROWS
		header = grid[0]
		rows = grid[1:]
		new_header, new_rows = expand_wide_matrix(header, rows, _layout())
		self.assertEqual(new_header, WIDE_MATRIX_SYNTHETIC_HEADER)
		self.assertEqual(
			new_rows,
			[
				["Test Mix Spec x20 STEMS", 40.0, "20", "PO-1001", "01/01/2026", "03/01/2026"],
				["Test Single Spec x10 STEMS", 30.0, "10", "PO-1002", "02/01/2026", "04/01/2026"],
			],
		)


class UnitTestOrderImportPlatformSchema(UnitTestCase):
	def test_layout_type_defaults_to_standard_rows(self):
		meta = frappe.get_meta("Order Import Platform")
		df = meta.get_field("layout_type")
		self.assertEqual(df.default, "Standard Rows")
		self.assertIn("Wide Matrix", df.options)

	def test_matrix_layout_fields_exist(self):
		meta = frappe.get_meta("Order Import Platform")
		for fieldname in (
			"matrix_group_label_row",
			"matrix_order_date_row",
			"matrix_delivery_date_row",
			"matrix_po_row",
			"matrix_items_start_row",
			"matrix_code_column",
			"matrix_description_column",
			"matrix_bunch_rate_column",
			"matrix_first_group_column",
			"matrix_group_column_width",
			"matrix_stop_value",
		):
			self.assertIsNotNone(meta.get_field(fieldname), f"missing field {fieldname}")

	def test_virtual_item_fields_include_bunches_and_bunch_rate(self):
		from upande_packhouse.api.order_import import VIRTUAL_ITEM_FIELDS

		names = {f["fieldname"] for f in VIRTUAL_ITEM_FIELDS}
		self.assertIn("_bunches_ordered", names)
		self.assertIn("_bunch_rate", names)
