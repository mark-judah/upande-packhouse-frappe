# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class BucketRequestTrip(Document):
	# Links are checked before any hook (before_validate included), so the clean-up
	# runs at the start of save/insert.
	def save(self, *args, **kwargs):
		self.drop_deleted_orders()
		return super().save(*args, **kwargs)

	def insert(self, *args, **kwargs):
		self.drop_deleted_orders()
		return super().insert(*args, **kwargs)

	def drop_deleted_orders(self):
		"""An Order Pick List deleted after it was planned would fail every later save of
		the trip (its link no longer resolves) — including the save that records the
		buckets the farm app loads. Its rows are dropped instead, with a note."""
		rows = list(self.get("orders") or []) + list(self.get("trip_buckets") or [])
		opls = {r.order_pick_list for r in rows if r.order_pick_list}
		if not opls:
			return
		gone = opls - set(
			frappe.get_all("Order Pick List", filters={"name": ["in", list(opls)]}, pluck="name")
		)
		if not gone:
			return
		self.set("orders", [r for r in self.orders if r.order_pick_list not in gone])
		self.set("trip_buckets", [r for r in self.trip_buckets if r.order_pick_list not in gone])
		for table in (self.orders, self.trip_buckets):
			for i, r in enumerate(table, 1):
				r.idx = i
		self.total_buckets = sum(int(o.buckets or 0) for o in self.orders)
		self.total_stems = sum(int(o.stems or 0) for o in self.orders)
		self.loaded_buckets = sum(int(o.loaded_buckets or 0) for o in self.orders)
		if not self.is_new():
			self.add_comment("Info", "Dropped deleted pick list(s) {0}".format(", ".join(sorted(gone))))
