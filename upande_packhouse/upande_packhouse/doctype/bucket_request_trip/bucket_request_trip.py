# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
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

	def validate(self):
		self.validate_no_double_booking()

	def validate_no_double_booking(self):
		"""Backstop for every planning / loading path: a bucket rides one trip, and the
		same open buckets are never planned on two trips (or two trucks)."""
		from upande_packhouse.api import transfer_control as tc

		before = self.get_doc_before_save()
		# 1. Buckets newly recorded on this trip must not be on another trip still on the road.
		old = {(r.order_pick_list, (r.bucket or "").upper()) for r in (before.trip_buckets if before else [])}
		new = {
			(r.bucket or "").upper()
			for r in self.get("trip_buckets") or []
			if r.bucket and (r.order_pick_list, (r.bucket or "").upper()) not in old and not r.off_truck
		}
		if new and self.status != "Received":
			for bucket, where in tc._bucket_trip_rows(new).items():
				other = [w for w in where if w.trip != self.name]
				if other:
					frappe.throw(
						_("Bucket {0} is already on trip {1} ({2}).").format(
							bucket, other[0].trip, other[0].vehicle
						),
						title=_("Bucket already on a trip"),
					)
		# 2. More buckets planned for an (order, farm) than are still free.
		if self.status not in tc.ACTIVE_TRIP_STATUSES:
			return
		was = {}
		for o in before.orders if before else []:
			k = (o.order_pick_list, o.farm or "")
			was[k] = was.get(k, 0) + int(o.buckets or 0)
		want, loaded = {}, {}
		for o in self.get("orders") or []:
			k = (o.order_pick_list, o.farm or "")
			want[k] = want.get(k, 0) + int(o.buckets or 0)
			loaded[k] = loaded.get(k, 0) + int(o.loaded_buckets or 0)
		grew = [k for k, n in want.items() if n > was.get(k, 0) and k[0]]
		if not grew:
			return
		opls = list({k[0] for k in grew})
		free_open = tc._open_counts(opls)
		claims = tc._trip_claims(opls, exclude_trip=None if self.is_new() else self.name)
		for k in grew:
			need = max(0, want[k] - loaded.get(k, 0))
			free = free_open.get(k, 0) - claims.get(k, 0)
			if need > free:
				frappe.throw(
					_(
						"{0} @ {1}: {2} bucket(s) planned but only {3} still free — the rest are on another trip."
					).format(k[0], k[1], need, max(0, free)),
					title=_("Already planned on another trip"),
				)

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
