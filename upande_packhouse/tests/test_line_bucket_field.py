"""A site without `Stock Entry Detail.custom_bucket_id` gets it on migrate: without it a release posts one Stock
Entry per bucket (199 one-row entries for 16 orders on live) instead of one per variety and length."""

from unittest.mock import patch

from frappe.tests import UnitTestCase

from upande_packhouse import stock_movement as sm


class UnitTestLineBucketField(UnitTestCase):
	def test_created_when_missing(self):
		with (
			patch.object(sm, "line_has_bucket", return_value=False),
			patch.object(sm.frappe.db, "exists", return_value=True),
			patch.object(sm.frappe.db, "has_column", return_value=True),
			patch("frappe.custom.doctype.custom_field.custom_field.create_custom_field") as create,
		):
			sm.ensure_line_bucket_field()
		dt, df = create.call_args[0]
		self.assertEqual(dt, "Stock Entry Detail")
		self.assertEqual(
			(df["fieldname"], df["fieldtype"], df["options"], df["insert_after"]),
			("custom_bucket_id", "Link", "Bucket QR Code", "custom_stem_length"),
		)

	def test_left_alone_when_present(self):
		with (
			patch.object(sm, "line_has_bucket", return_value=True),
			patch("frappe.custom.doctype.custom_field.custom_field.create_custom_field") as create,
		):
			sm.ensure_line_bucket_field()
		create.assert_not_called()
