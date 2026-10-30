import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse import bucket_reuse as br


class IntegrationTestBucketReuse(IntegrationTestCase):
	"""An allocation made before its bucket was harvested again is from a previous life of the bucket."""

	def _pick(self):
		rows = br.stale_allocations()
		if not rows:
			self.skipTest("needs an unissued allocation on a bucket that was harvested again")
		return rows[0]

	def test_stale_means_harvested_after_allocation(self):
		r = self._pick()
		last = frappe.db.sql(
			"SELECT MAX(creation) FROM `tabStock Entry` WHERE stock_entry_type = 'Harvesting' AND docstatus != 2 AND custom_bucket_id = %s",
			r.bucket_id,
		)[0][0]
		self.assertGreater(last, r.creation)

	def test_one_bucket_only_lists_its_own(self):
		r = self._pick()
		self.assertTrue(all(x.bucket_id == r.bucket_id for x in br.stale_allocations(r.bucket_id)))

	def test_allocation_after_the_harvest_is_current(self):
		r = self._pick()
		name = r.ba
		frappe.db.set_value("Bucket Allocations", name, "creation", frappe.utils.add_days(r.creation, 400), update_modified=False)
		try:
			self.assertNotIn(name, [x.ba for x in br.stale_allocations(r.bucket_id)])
		finally:
			frappe.db.set_value("Bucket Allocations", name, "creation", r.creation, update_modified=False)

	def test_delivered_lines_are_left_for_a_person(self):
		rows = [x for x in br.stale_allocations() if x.delivered or (x.per_delivered or 0) >= 100]
		if not rows:
			self.skipTest("no delivered stale line")
		out = br.release_reused_bucket(rows[0].bucket_id)
		self.assertGreaterEqual(out["skipped"], 1)
		self.assertTrue(frappe.db.exists("Bucket Allocations", {"name": rows[0].ba, "cancelled": 0}))
