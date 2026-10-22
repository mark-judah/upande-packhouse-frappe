# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class BucketRequestTrip(Document):
	# Links are checked before any hook (before_validate included), so the clean-up
	# runs at the start of save/insert.
	def save(self, *args, **kwargs):
		from upande_packhouse.api import transfer_control as tc

		self.drop_deleted_orders()
		self.drop_due_orders()
		# A farm the truck has left (any dispatch path, or a departed stop): its rows are
		# cut to what was loaded, and the rest goes on the truck's next run there.
		left = tc.trim_left_farms(self)
		self.sync_riding_orders()
		out = super().save(*args, **kwargs)
		if left:
			tc.carry_left_behind(self, left)
		return out

	def insert(self, *args, **kwargs):
		self.drop_deleted_orders()
		self.drop_due_orders()
		self.sync_riding_orders()
		return super().insert(*args, **kwargs)

	def sync_riding_orders(self):
		"""Every order a bucket on this trip is shared with is on the trip too: a bucket
		several orders take stems from is planned once, under its lead order, and the
		others ride with it. Each rider gets a row of its own (0 buckets, nothing counted)
		so every screen and the farm app see the order on this trip -- they all read the
		trip's order rows."""
		from upande_packhouse.api import transfer_control as tc

		if self.status not in tc.ACTIVE_TRIP_STATUSES + ("Dispatched",):
			return
		hub = tc.transfer_hub(required=False)
		planned = {
			(o.order_pick_list, o.farm or "")
			for o in self.get("orders") or []
			if o.order_pick_list and o.farm and o.farm != hub and int(o.buckets or 0) > 0
		}
		riders = {}
		if planned:
			buckets = frappe.db.sql_list(
				"""SELECT DISTINCT UPPER(pli.bucket) FROM `tabPick List Item` pli
				WHERE pli.parenttype = 'Order Pick List' AND pli.parent IN %(o)s
				  AND IFNULL(pli.bucket, '') != '' AND IFNULL(pli.shelved, 0) = 0
				  AND (pli.awaiting_transfer = 1 OR pli.loaded_in_trolley = 1 OR pli.in_transit = 1)""",
				{"o": tuple({k[0] for k in planned})},
			)
			for (_bucket, farm), g in tc.shared_buckets(buckets).items():
				if (g["lead"], farm) not in planned:
					continue
				for opl in g["opls"]:
					if opl != g["lead"]:
						riders.setdefault((opl, farm), g["lead"])
		have = {(o.order_pick_list, o.farm or "") for o in self.get("orders") or []}
		new = [k for k in riders if k not in have]
		if not new:
			return
		opls = {
			r.name: r
			for r in frappe.get_all(
				"Order Pick List",
				filters={"name": ["in", list({k[0] for k in new})]},
				fields=["name", "order_name", "sales_order"],
			)
		}
		sos = [r.sales_order for r in opls.values() if r.sales_order]
		customers = (
			dict(
				frappe.get_all(
					"Sales Order", filters={"name": ["in", sos]}, fields=["name", "customer"], as_list=True
				)
			)
			if sos
			else {}
		)
		for opl, farm in new:
			m = opls.get(opl)
			if m:
				self.append(
					"orders",
					{
						"order_pick_list": opl,
						"order_name": m.order_name or opl,
						"customer": customers.get(m.sales_order) or "",
						"farm": farm,
						"buckets": 0,
						"stems": 0,
						"loaded_buckets": 0,
						"loaded_stems": 0,
					},
				)

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
		want = {}
		for o in self.get("orders") or []:
			k = (o.order_pick_list, o.farm or "")
			want[k] = want.get(k, 0) + int(o.buckets or 0)
		grew = [k for k, n in want.items() if n > was.get(k, 0) and k[0]]
		if not grew:
			return
		opls = list({k[0] for k in grew})
		free_open = tc._open_counts(opls)
		# What this trip claims (as _trip_claims counts it): its plan or what is loaded for
		# it (truck, trip record or trolley), whichever is more. Loaded buckets are still
		# open, so they are not taken off the plan.
		loads = {} if self.is_new() else tc._trip_loads(opls).get(self.name, {})
		need = {k: max(n, loads.get(k, 0)) for k, n in want.items() if k[0]}
		# By physical bucket: a shared bucket this or another trip already carries under
		# another of its orders is not free for this one.
		claims = tc._bucket_claims(
			opls,
			exclude_trip=None if self.is_new() else self.name,
			planning=need,
		)
		for k in grew:
			need_k = need[k]
			free = free_open.get(k, 0) - claims.get(k, 0)
			if need_k > free:
				frappe.throw(
					_(
						"{0} @ {1}: {2} bucket(s) planned but only {3} still free — the rest are on another trip."
					).format(k[0], k[1], need_k, max(0, free)),
					title=_("Already planned on another trip"),
				)

	def drop_due_orders(self):
		"""A trip only carries orders delivering after its trip date (tomorrow or the day
		after): an order already due is dropped on every save -- Distribute, automatic
		scheduling or a manual add -- unless its buckets are already loaded on the truck."""
		from upande_packhouse.api import transfer_control as tc

		if self.status not in tc.ACTIVE_TRIP_STATUSES or not self.trip_date:
			return
		opls = {o.order_pick_list for o in self.get("orders") or [] if o.order_pick_list}
		if not opls:
			return
		due = set(
			frappe.db.sql_list(
				"""SELECT opl.name FROM `tabOrder Pick List` opl
				JOIN `tabSales Order` so ON so.name = opl.sales_order
				WHERE opl.name IN %(o)s AND so.delivery_date <= %(d)s""",
				{"o": tuple(opls), "d": self.trip_date},
			)
		)
		dropped = [o for o in self.orders if o.order_pick_list in due and not int(o.loaded_buckets or 0)]
		if not dropped:
			return
		self.set("orders", [o for o in self.orders if o not in dropped])
		for i, r in enumerate(self.orders, 1):
			r.idx = i
		self.total_buckets = sum(int(o.buckets or 0) for o in self.orders)
		self.total_stems = sum(int(o.stems or 0) for o in self.orders)
		if not self.is_new():
			self.add_comment(
				"Info",
				"Dropped order(s) delivering on or before {0}: {1}".format(
					self.trip_date, ", ".join(sorted({o.order_name or o.order_pick_list for o in dropped}))
				),
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
