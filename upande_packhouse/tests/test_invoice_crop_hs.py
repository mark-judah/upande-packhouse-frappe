"""Roses invoice: crop naming and HS codes (item_groups.crop_name / hs_code)."""

import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.item_groups import crop_name, default_hs_code, hs_code


class TestInvoiceCropHs(IntegrationTestCase):
	def test_crop_names(self):
		self.assertEqual(crop_name("Standard Roses - Premium"), "Std Rose")
		self.assertEqual(crop_name("Spray Roses - The Classics"), "Spr Rose")
		self.assertEqual(crop_name("Lepidium - Regular"), "Lepidium")
		self.assertEqual(crop_name("Limonium - Premium"), "Limonium")
		self.assertEqual(crop_name("Fillers - Eryngium"), "Fillers")
		self.assertEqual(crop_name("Lepidium"), "Lepidium")  # a parent group is its own crop

	def test_default_hs_codes(self):
		self.assertEqual(default_hs_code("Standard Roses - Retail"), "060311")
		self.assertEqual(default_hs_code("Spray Roses - Garden"), "060311")
		self.assertEqual(default_hs_code("Chrysanthemums - Spray"), "060314")
		self.assertEqual(default_hs_code("Gypsophilla - Regular"), "060319")
		self.assertEqual(default_hs_code("Fillers - Hypericum"), "060319")
		self.assertEqual(default_hs_code("Fillers - Aster"), "")  # not in the list: left blank, not guessed

	def test_item_code_overrides_the_group_default(self):
		item = frappe.get_all(
			"Item", filters={"item_group": "Gypsophilla - Regular", "disabled": 0}, pluck="name", limit=1
		)
		if not item:
			self.skipTest("no Gypsophila item on this site")
		frappe.db.set_value("Item", item[0], "customs_tariff_number", "060390")
		frappe.clear_document_cache("Item", item[0])
		self.assertEqual(hs_code(item[0]), "060390")
