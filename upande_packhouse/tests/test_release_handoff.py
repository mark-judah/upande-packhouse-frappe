"""A release handed on past its time budget is taken by exactly one of the open page and the background worker:
the page goes on with it without waiting for a free worker, a closed page leaves it to the job, and no part of it
runs twice."""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from upande_packhouse.upande_packhouse.page.sales_allocation_v2 import day_allocation as da

LOC = "Kapkolia"


class UnitTestReleaseHandoff(UnitTestCase):
	def setUp(self):
		self.token = f"sa2_release:{LOC}:{frappe.generate_hash(length=10)}"
		self.payload = {"location": LOC, "sales_orders": ["SO-2"], "offset": 1, "total": 2, "carried": {}}
		frappe.cache.set_value(da._handoff_key(self.token), self.payload, expires_in_sec=60)

	def tearDown(self):
		frappe.cache.delete_value(da._handoff_key(self.token))
		frappe.cache.delete(frappe.cache.make_key(f"sa2_release_claim:{self.token}"))

	def test_only_the_first_claim_wins(self):
		self.assertTrue(da._claim_release(self.token, "page"))
		self.assertFalse(da._claim_release(self.token, "job"))
		self.assertFalse(da._claim_release(self.token, "page"))

	def test_the_page_takes_it_and_the_job_leaves_it(self):
		with patch.object(da, "_run_confirm", return_value={"done": True}) as run:
			out = da.continue_release(LOC, self.token)
			self.assertEqual(out, {"done": True})
			self.assertEqual(run.call_args.kwargs["budget"], da.RELEASE_BUDGET)
			self.assertEqual(run.call_args.kwargs["sales_orders"], ["SO-2"])
			self.assertIsNone(da._release_job(self.token))
			self.assertEqual(run.call_count, 1)

	def test_the_job_takes_it_and_the_page_only_follows(self):
		with patch.object(da, "_run_confirm", return_value={"done": True}) as run:
			da._release_job(self.token)
			self.assertEqual(da.continue_release(LOC, self.token), {"claimed": False})
			self.assertEqual(run.call_count, 1)
			self.assertIsNone(run.call_args.kwargs.get("budget"))

	def test_a_token_of_another_location_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			da.continue_release("Karen", self.token)

	def test_the_hand_on_is_kept_before_it_is_queued(self):
		order = []
		with (
			patch.object(
				da.frappe.cache, "set_value", side_effect=lambda k, *a, **kw: order.append(("keep", k))
			),
			patch.object(
				da.frappe, "enqueue", side_effect=lambda *a, **kw: order.append(("queue", kw["token"]))
			),
			patch.object(da, "_set_state"),
		):
			out = da._continue_release(
				{"location": LOC, "delivery_start": "2026-11-27"},
				[{"sales_order": "SO-1"}, {"sales_order": "SO-2"}],
				1,
				0,
				2,
				{},
			)
		self.assertEqual([x[0] for x in order], ["keep", "queue"])
		self.assertEqual(order[0][1], da._handoff_key(out["run"]))
		self.assertEqual(order[1][1], out["run"])


class UnitTestReleasedOnce(UnitTestCase):
	def test_an_order_no_longer_pending_is_left(self):
		with patch.object(da.frappe.db, "sql", return_value=[]) as q:
			self.assertFalse(da._still_pending({"name": "PA-1"}))
		self.assertIn("FOR UPDATE", q.call_args[0][0])
		with patch.object(da.frappe.db, "sql", return_value=[("PA-1",)]):
			self.assertTrue(da._still_pending({"name": "PA-1"}))
