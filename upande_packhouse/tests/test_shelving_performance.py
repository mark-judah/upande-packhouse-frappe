"""A receipt is matched to its bucket's first shelving in the same cold room, within its own cycle."""

from datetime import datetime, timedelta
from unittest import TestCase

from frappe import _dict

from upande_packhouse.api.v2 import shelving_performance as sp

T0 = datetime(2026, 11, 3, 8, 0)


def receipt(bucket, farm, at):
	return {"bucket": bucket, "farm": farm, "at": at, "source": "Receiving", "stems": 100}


def shelving(bucket, farm, at, by="a@x.com"):
	return _dict(bucket_id=bucket, farm=farm, shelved_on=at, shelved_by=by, stem_qty=100)


class TestShelvingMatch(TestCase):
	def test_first_shelving_in_the_same_cold_room(self):
		r = receipt("B1", "Kapkolia", T0)
		sp._match(
			[r],
			[
				shelving("b1", "Simotwo", T0 + timedelta(minutes=5)),
				shelving("B1", "Kapkolia", T0 + timedelta(minutes=40)),
				shelving("B1", "Kapkolia", T0 + timedelta(hours=5)),
			],
		)
		self.assertEqual(r["minutes"], 40)

	def test_scan_just_before_the_receipt_counts_as_on_time(self):
		r = receipt("B1", "Kapkolia", T0)
		sp._match([r], [shelving("B1", "Kapkolia", T0 - timedelta(minutes=10))])
		self.assertEqual(r["minutes"], 0)

	def test_a_reused_bucket_is_not_shelved_by_its_next_harvest(self):
		first, second = receipt("B1", "Chepsito", T0), receipt("B1", "Chepsito", T0 + timedelta(days=1))
		sp._match([first, second], [shelving("B1", "Chepsito", T0 + timedelta(days=1, minutes=20))])
		self.assertIsNone(first["shelving"])
		self.assertEqual(second["minutes"], 20)

	def test_nothing_after_three_days(self):
		r = receipt("B1", "Karen", T0)
		sp._match([r], [shelving("B1", "Karen", T0 + timedelta(days=4))])
		self.assertIsNone(r["minutes"])

	def test_summary(self):
		rs = [{"minutes": 30}, {"minutes": 200}, {"minutes": None}, {"minutes": 90}]
		s = sp._summary(rs, 120)
		self.assertEqual((s["received"], s["shelved"], s["on_time"]), (4, 3, 2))
		self.assertEqual((s["shelved_pct"], s["on_time_pct"], s["median_minutes"]), (75.0, 50.0, 90))
