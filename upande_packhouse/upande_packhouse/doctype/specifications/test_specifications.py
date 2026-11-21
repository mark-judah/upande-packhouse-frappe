# Copyright (c) 2026, Upande and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import add_days, nowdate

from upande_packhouse.upande_packhouse.doctype.specifications.specifications import Specifications


def _spec(status, expiry, before_status=None):
	doc = frappe.new_doc("Specifications")
	doc.spec_type = "Temporary"
	doc.status = status
	doc.valid_from = add_days(nowdate(), -10)
	doc.expiry_date = expiry
	before = SimpleNamespace(status=before_status) if before_status else None
	return doc, before


class UnitTestSpecificationStatus(UnitTestCase):
	def _run(self, doc, before):
		with patch.object(Specifications, "get_doc_before_save", return_value=before):
			doc.validate_temporary_dates()

	def test_inactive_spec_can_be_made_active_before_its_expiry(self):
		doc, before = _spec("Active", add_days(nowdate(), 3), before_status="Inactive")
		self._run(doc, before)
		self.assertEqual(doc.status, "Active")

	def test_active_on_the_expiry_day_is_allowed(self):
		doc, before = _spec("Active", nowdate(), before_status="Inactive")
		self._run(doc, before)
		self.assertEqual(doc.status, "Active")

	def test_expired_spec_cannot_be_switched_back_to_active(self):
		doc, before = _spec("Active", add_days(nowdate(), -1), before_status="Inactive")
		with self.assertRaises(frappe.ValidationError):
			self._run(doc, before)

	def test_lapsed_active_spec_is_saved_inactive(self):
		doc, before = _spec("Active", add_days(nowdate(), -1), before_status="Active")
		self._run(doc, before)
		self.assertEqual(doc.status, "Inactive")

	def test_permanent_spec_is_untouched(self):
		doc, before = _spec("Active", add_days(nowdate(), -1), before_status="Inactive")
		doc.spec_type = "Permanent"
		self._run(doc, before)
		self.assertEqual(doc.status, "Active")
