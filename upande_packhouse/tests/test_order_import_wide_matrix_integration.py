import json

import frappe
from frappe.tests import IntegrationTestCase

CUSTOMER = "Test Wide Matrix Integration Customer"
PLATFORM = "Test Wide Matrix Integration Platform"
CUT_STAGE = "Test Wide Matrix Integration Cut Stage"
BOX_TYPE = "Test Wide Matrix Integration Box Type"
STEM_LENGTH = "52cm"

# Same synthetic shape as Task 1's fixture, with LINE text set to exactly
# match a real Specification (no rename/alias flow involved - that existing
# mechanism is out of scope here, already covered by its own tests).
#
# Order/delivery dates deliberately use day-of-month > 12 (15-18, not the
# plan's literal 01-04), unlike Task 1's own pure-pivot fixture which never
# runs these through a real date parser. buildPreview's coerce_mapped_value
# calls frappe.utils.getdate(raw) with no parse_day_first=True, so despite
# its own docstring claiming to match "this system's own date_format" (this
# bench's System Settings.date_format is dd-mm-yyyy), it actually parses
# ambiguous dd/mm strings as MM/DD (confirmed directly: getdate("03/01/2026")
# == 2026-03-01, not 2026-01-03) - a pre-existing discrepancy in code Task 5
# doesn't touch, out of scope to fix here. Day values above 12 sidestep the
# ambiguity entirely (dateutil's parser swaps an impossible month back to
# day-first automatically), so the test exercises real date coercion without
# depending on that unrelated bug.
TWO_GROUP_ROWS = [
	["TESTCO01 PO lines by date", "GBP"],
	["TESTCUST"],
	["", "", "", "", "Alpha", "", "Beta"],
	["", "", "", "", "15/01/2026", "", "16/01/2026"],
	["", "", "", "", "17/01/2026", "", "18/01/2026"],
	["", "", "", "", "PO-WM-1001", "confirmation", "PO-WM-1002", "confirmation"],
	["MIXCODE", "Test Wide Matrix Integration Mix Spec", "1.0", "10", "30", "30", "0", "0"],
	["SINGLECODE", "Test Wide Matrix Integration Single Spec", "0.5", "5", "0", "0", "20", "20"],
	["Total", "", "", "", "30", "", "20"],
]


class IntegrationTestWideMatrixBuildPreview(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Customer", CUSTOMER):
			# This bench enforces several Customer fields as mandatory via DB
			# Property Setters/Custom Fields that aren't visible in the base
			# doctype JSON (default_currency, default_price_list,
			# custom_business_unit, payment_terms, accounts) - not reflected
			# in the plan's literal fixture, which only sets customer_name.
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": CUSTOMER,
					"default_currency": "GBP",
					"default_price_list": "Standard Selling",
					"custom_business_unit": "Roses",
					"payment_terms": "Cash before collection",
					"accounts": [{"company": "Karen Roses"}],
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Cut Stage", CUT_STAGE):
			frappe.get_doc({"doctype": "Cut Stage", "cutstage": CUT_STAGE}).insert(ignore_permissions=True)
		if not frappe.db.exists("Box Type", BOX_TYPE):
			frappe.get_doc({"doctype": "Box Type", "name": BOX_TYPE, "box_name": BOX_TYPE}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("Stem Length", STEM_LENGTH):
			frappe.get_doc({"doctype": "Stem Length", "length": STEM_LENGTH}).insert(ignore_permissions=True)
		for color in ("Test Wide Matrix Integration Red", "Test Wide Matrix Integration Blue"):
			if not frappe.db.exists("Colors", color):
				frappe.get_doc({"doctype": "Colors", "color_name": color}).insert(ignore_permissions=True)
		# Spec Approved Variety's own "colour" field is fetch_from
		# variety.custom_color with fetch_if_empty unset (False), so Frappe
		# always re-fetches it on save regardless of what a row sets directly
		# - each test Item needs its own custom_color set so the
		# Specification ends up with the colour this fixture intends, rather
		# than every row collapsing to the same blank colour (see
		# test_order_import_spec_row_bunches.py for the same pattern).
		for item, color in (
			("Test Wide Matrix Integration Variety Red", "Test Wide Matrix Integration Red"),
			("Test Wide Matrix Integration Variety Blue", "Test Wide Matrix Integration Blue"),
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
		if not frappe.db.exists("Item", "Test Wide Matrix Integration Variety Single"):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": "Test Wide Matrix Integration Variety Single",
					"item_name": "Test Wide Matrix Integration Variety Single",
					"item_group": frappe.db.get_value("Item Group", {}, "name"),
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Specifications", "Test Wide Matrix Integration Mix Spec"):
			frappe.get_doc(
				{
					"doctype": "Specifications",
					"spec_name": "Test Wide Matrix Integration Mix Spec",
					"ftnft": "FT",
					"spec_type": "Permanent",
					"cut_stage": CUT_STAGE,
					"defoliation_length": "20",
					"box_assortment": "Mixed Box",
					"min_colours_per_box": 2,
					"max_colours_per_box": 2,
					"box_type": BOX_TYPE,
					"approved_varieties": [
						{
							"variety": "Test Wide Matrix Integration Variety Red",
							"is_primary": 1,
						},
						{
							"variety": "Test Wide Matrix Integration Variety Blue",
							"is_primary": 1,
						},
					],
					"box_items": [
						{
							"bunch_type": "Mono Bunch",
							"stems_per_bunch": 5,
							"length": STEM_LENGTH,
							"bunches_per_box": 10,
							"pack_rate": 50,
						},
						{
							"bunch_type": "Mono Bunch",
							"stems_per_bunch": 5,
							"length": STEM_LENGTH,
							"bunches_per_box": 10,
							"pack_rate": 50,
						},
					],
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Specifications", "Test Wide Matrix Integration Single Spec"):
			frappe.get_doc(
				{
					"doctype": "Specifications",
					"spec_name": "Test Wide Matrix Integration Single Spec",
					"ftnft": "FT",
					"spec_type": "Permanent",
					"cut_stage": CUT_STAGE,
					"defoliation_length": "20",
					"box_assortment": "Mono Box",
					"box_type": BOX_TYPE,
					"approved_varieties": [
						{
							"variety": "Test Wide Matrix Integration Variety Single",
							"is_primary": 1,
						}
					],
					"box_items": [
						{
							"bunch_type": "Mono Bunch",
							"stems_per_bunch": 5,
							"length": STEM_LENGTH,
							"bunches_per_box": 4,
							"pack_rate": 20,
						}
					],
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Order Import Platform", PLATFORM):
			frappe.get_doc(
				{
					"doctype": "Order Import Platform",
					"platform_name": PLATFORM,
					"file_type": "Excel",
					"layout_type": "Wide Matrix",
					"multi_order_column": "PO Number",
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
					"default_field_mappings": [
						{
							"source_column": "Specification",
							"target_doctype": "Sales Order Item",
							"target_fieldname": "custom_line",
						},
						{
							"source_column": "Bunches Ordered",
							"target_doctype": "Sales Order Item",
							"target_fieldname": "_bunches_ordered",
						},
						{
							"source_column": "Bunch Rate",
							"target_doctype": "Sales Order Item",
							"target_fieldname": "_bunch_rate",
						},
						{
							"source_column": "PO Number",
							"target_doctype": "Sales Order",
							"target_fieldname": "po_no",
						},
						{
							"source_column": "Order Date",
							"target_doctype": "Sales Order",
							"target_fieldname": "transaction_date",
						},
						{
							"source_column": "Delivery Date",
							"target_doctype": "Sales Order",
							"target_fieldname": "delivery_date",
						},
					],
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_build_preview_splits_into_one_order_per_po_group(self):
		from upande_packhouse.api.order_import import buildPreview

		frappe.form_dict.data = json.dumps(
			{"customer": CUSTOMER, "platform": PLATFORM, "header": [], "rows": TWO_GROUP_ROWS}
		)
		buildPreview()
		result = frappe.response["message"]
		self.assertTrue(result["success"], result.get("error"))
		orders = result["orders"]
		self.assertEqual(len(orders), 2)

		by_po = {o["header"]["po_no"]: o for o in orders}
		self.assertEqual(set(by_po), {"PO-WM-1001", "PO-WM-1002"})

		mix_order = by_po["PO-WM-1001"]
		self.assertEqual(str(mix_order["header"]["transaction_date"]), "2026-01-15")
		self.assertEqual(str(mix_order["header"]["delivery_date"]), "2026-01-17")
		self.assertEqual(len(mix_order["rows"]), 2)  # mix spec -> one row per colour
		self.assertEqual({r["item_code"] for r in mix_order["rows"]}, {
			"Test Wide Matrix Integration Variety Red",
			"Test Wide Matrix Integration Variety Blue",
		})
		# 30 bunches / 10 per box = 3 boxes, matching bunch rate (10 == spec's 5? no -
		# this fixture's file Bunch Rate column is "10", spec's stems_per_bunch is 5,
		# so a mismatch warning is expected here deliberately.
		self.assertIn("_bunch_rate_mismatch", mix_order["rows"][0])

		single_order = by_po["PO-WM-1002"]
		self.assertEqual(len(single_order["rows"]), 1)
		self.assertEqual(single_order["rows"][0]["item_code"], "Test Wide Matrix Integration Variety Single")
		# 20 bunches / 4 per box = 5 boxes.
		self.assertEqual(single_order["rows"][0]["custom_number_of_boxes"], 5)
		# File Bunch Rate "5" matches this spec's stems_per_bunch (5) - no warning.
		self.assertNotIn("_bunch_rate_mismatch", single_order["rows"][0])

		self.assertEqual(result["unresolved_customers"], [])
