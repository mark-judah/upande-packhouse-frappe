from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse import roses_invoice

# Production Settings > Dispatch and Invoicing > Create Sales Invoice When a Delivery Note
# Is Submitted: off (the default) leaves the invoicing to the users, who pick the Delivery
# Notes to consolidate; on makes one draft invoice per submitted Roses Delivery Note.
# The database and the mapper are mocked: these run on a bare site (CI) as well.


class UnitTestDeliveryNoteAutoInvoice(UnitTestCase):
	def _submit(self, setting, business_unit="Roses"):
		class DN:
			name = "MAT-DN-1"
			items = ()

			def get(self, key, default=None):
				return {"business_unit": business_unit}.get(key, default)

		doc = DN()
		with (
			patch.object(roses_invoice.frappe.db, "get_single_value", return_value=setting),
			patch.object(roses_invoice.frappe.db, "sql", return_value=[]),
			patch.object(roses_invoice, "make_sales_invoice") as mapper,
		):
			roses_invoice.delivery_note_on_submit(doc)
		return mapper

	def test_setting_off_makes_no_invoice(self):
		self.assertFalse(self._submit(0).called)

	def test_setting_never_saved_makes_no_invoice(self):
		self.assertFalse(self._submit(None).called)

	def test_non_roses_never_invoices_even_when_on(self):
		self.assertFalse(self._submit(1, business_unit="Other").called)

	def test_setting_on_builds_the_invoice(self):
		self.assertTrue(self._submit(1).called)
