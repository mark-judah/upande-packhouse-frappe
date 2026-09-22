import frappe
from frappe.tests import IntegrationTestCase

# These exercise the shelving flow against a real farm's master data: a
# Company, the greenhouse and receiving cold store its stems move between, the
# cost centre ERPNext names after it, and a variety Item. A bare
# Frappe + ERPNext site -- which is exactly what CI installs -- carries none of
# it, and standing an ERPNext Company and its chart of accounts up inside these
# tests would be a different test from the one they are. So they SKIP on a site
# without the data (CI, a fresh dev site) and run in full on one with it.
COMPANY = "Karen Roses"
COST_CENTER = "Karen Roses - KR"
GREENHOUSE_WAREHOUSE = "Karen GH 04 - KR"
RECEIVING_WAREHOUSE = "Karen Receiving Cold Store - KR"
VARIETY_ITEM = "Reflex"

REQUIRED_RECORDS = (
	("Company", COMPANY),
	("Cost Center", COST_CENTER),
	("Warehouse", GREENHOUSE_WAREHOUSE),
	("Warehouse", RECEIVING_WAREHOUSE),
	("Item", VARIETY_ITEM),
)


def missing_master_data():
	"""The records above this site hasn't got, named for the skip message."""
	return [
		f"{doctype} '{name}'" for doctype, name in REQUIRED_RECORDS if not frappe.db.exists(doctype, name)
	]


class IntegrationTestShelfOperationsPackhouse(IntegrationTestCase):
	def setUp(self):
		missing = missing_master_data()
		if missing:
			self.skipTest("site has no " + ", ".join(missing))

		self.farm = "Test Shelf Ops Farm"
		if not frappe.db.exists("Farm", self.farm):
			frappe.get_doc(
				{
					"doctype": "Farm",
					"farm_name": self.farm,
					"company": COMPANY,
					"abbreviation": "TSOF",
					"farm_type": [{"farm_type": "Has Greenhouses"}],
				}
			).insert(ignore_permissions=True)
		self.bucket_id = "TEST-BUCKET-002"
		if not frappe.db.exists("Bucket QR Code", self.bucket_id):
			frappe.get_doc(
				{"doctype": "Bucket QR Code", "id": self.bucket_id, "item_code": VARIETY_ITEM}
			).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Stock Entry", {"custom_bucket_id": self.bucket_id})
		frappe.db.delete("Shelving Log", {"bucket_id": self.bucket_id})
		frappe.db.delete("Shelf Item", {"bucket_id": self.bucket_id})
		frappe.db.commit()

	def test_shelve_bucket_writes_shelved_log_row(self):
		shelf_id = "TEST-SHELF-B"
		if not frappe.db.exists("Shelf", shelf_id):
			frappe.get_doc({"doctype": "Shelf", "shelf_id": shelf_id, "farm": self.farm}).insert(
				ignore_permissions=True
			)

		shelf_doc = frappe.get_doc("Shelf", shelf_id)
		new_item = shelf_doc.append("items", {})
		new_item.bucket_id = self.bucket_id
		new_item.variety = VARIETY_ITEM
		new_item.stem_qty = 30
		new_item.farm = self.farm
		new_item.date_added = frappe.utils.now_datetime()
		shelf_doc.save(ignore_permissions=True)
		frappe.db.commit()

		from upande_packhouse.mobile.api import _write_shelved_log

		_write_shelved_log(new_item, shelf_id, self.farm)
		frappe.db.commit()

		log = frappe.get_all(
			"Shelving Log",
			filters={"bucket_id": self.bucket_id, "reason": "Shelved"},
			fields=["name", "shelf", "shelf_item", "shelved_by", "shelved_on"],
		)
		self.assertEqual(len(log), 1)
		self.assertEqual(log[0].shelf, shelf_id)
		self.assertEqual(log[0].shelf_item, new_item.name)
		self.assertEqual(log[0].shelved_by, frappe.session.user)
		self.assertTrue(log[0].shelved_on)

	def test_shelve_bucket_end_to_end_writes_shelved_log_row(self):
		"""Exercises the real shelveBucket() wiring (not just the extracted
		helper) -- the actual bug this task fixes. Same real preconditions
		shelveBucket enforces: a submitted Harvesting entry within 1 day of a
		submitted Receiving entry, both same-day so the staleness gate passes."""
		shelf_id = "TEST-SHELF-G"
		if not frappe.db.exists("Shelf", shelf_id):
			frappe.get_doc({"doctype": "Shelf", "shelf_id": shelf_id, "farm": self.farm}).insert(
				ignore_permissions=True
			)

		today = frappe.utils.today()
		harvest = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Harvesting",
				"purpose": "Material Receipt",
				"company": COMPANY,
				"posting_date": today,
				"custom_bucket_id": self.bucket_id,
				"items": [
					{
						"item_code": VARIETY_ITEM,
						"qty": 20,
						"t_warehouse": GREENHOUSE_WAREHOUSE,
						"uom": "Stems",
						"allow_zero_valuation_rate": 1,
						"cost_center": COST_CENTER,
					}
				],
			}
		)
		harvest.insert(ignore_permissions=True)
		harvest.submit()

		receiving = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Receiving",
				"purpose": "Material Transfer",
				"company": COMPANY,
				"posting_date": today,
				"set_posting_time": 1,
				"custom_bucket_id": self.bucket_id,
				"items": [
					{
						"item_code": VARIETY_ITEM,
						"qty": 20,
						"uom": "Stems",
						"s_warehouse": GREENHOUSE_WAREHOUSE,
						"t_warehouse": RECEIVING_WAREHOUSE,
						"custom_stem_length": "52cm",
						"allow_zero_valuation_rate": 1,
						"cost_center": COST_CENTER,
					}
				],
			}
		)
		receiving.insert(ignore_permissions=True)
		receiving.submit()
		frappe.db.commit()

		from upande_packhouse.mobile.api import shelveBucket

		frappe.local.form_dict = frappe._dict({})
		frappe.request = frappe._dict(
			get_json=lambda: {"shelf_id": shelf_id, "bucket_id": self.bucket_id, "farm": self.farm}
		)
		frappe.response = frappe._dict()
		shelveBucket()

		self.assertEqual(frappe.response["data"]["status"], "success")

		logs = frappe.get_all(
			"Shelving Log",
			filters={"bucket_id": self.bucket_id, "reason": "Shelved"},
			fields=["shelf", "shelf_item", "shelved_by", "shelved_on", "removed_on"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].shelf, shelf_id)
		self.assertTrue(logs[0].shelf_item)
		self.assertEqual(logs[0].shelved_by, frappe.session.user)
		self.assertTrue(logs[0].shelved_on)
		self.assertFalse(logs[0].removed_on)

		frappe.db.delete("Stock Entry", {"custom_bucket_id": self.bucket_id})
		frappe.db.commit()
