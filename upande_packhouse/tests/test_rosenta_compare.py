"""Rosenta comparison (api/v2/rosenta_compare.py). TEMPORARY: delete with the module.

The helper and session tests need no Rosenta and no data. The last class runs the whole call on the
local site with the Rosenta side mocked; the ERPNext side is Packhouse v2 itself.
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from upande_packhouse.api.v2 import rosenta_compare as rc


class DictStore:
	def __init__(self, value=None):
		self.value = value
		self.sets = 0

	def get(self):
		return self.value

	def set(self, value):
		self.value = value
		self.sets += 1


DATA = {"Eldama": {}}


def _reply(payload):
	r = MagicMock()
	if payload is None:
		r.json.side_effect = ValueError("not json")  # "not allowed" followed by the sign-in page
	else:
		r.json.return_value = payload
	return r


class TestHelpers(IntegrationTestCase):
	def test_name_tokens_drop_date_codes_and_order_numbers(self):
		self.assertEqual(rc._name_tokens("SANDY  34726-04809"), {"SANDY"})
		self.assertEqual(rc._name_tokens("IGO 2711 24726"), {"IGO", "2711"})

	def test_key_ignores_case_and_punctuation(self):
		self.assertEqual(rc._key("Flamingo Flowers Ltd,"), "FLAMINGO FLOWERS LTD")

	def test_status_words(self):
		w = rc._status_word
		self.assertEqual(w(False, False, 0, 0, 0), "-")
		self.assertEqual(w(False, True, 0, 5, 0), "Only in ERPNext")
		self.assertEqual(w(True, False, 5, 0, 0), "Only in Rosenta")
		self.assertEqual(w(True, True, 5, 5, 0), "Matches")
		self.assertEqual(w(True, True, 6, 5, 0), "ERPNext lower")
		self.assertEqual(w(True, True, 4, 5, 0), "Rosenta lower")
		self.assertEqual(w(True, True, 6, 5, 1), "Matches")

	def test_customer_mapping(self):
		c = rc._Customers(["FLAMINGO UK", "AOA VENTURES LTD-KAZ", "Oasis", "FLOWER AXIS LIMITED"])
		self.assertEqual(c.map("Oasis"), "Oasis")  # same name
		self.assertEqual(c.map("Flamingo Flowers Ltd,"), "FLAMINGO UK")  # override
		self.assertEqual(c.map("AOA VENTURES LTD-KAZAKHSTAN"), "AOA VENTURES LTD-KAZ")  # override
		self.assertIsNone(c.map("Zami BV"))  # never guessed

	def test_pairing_by_name_then_boxes(self):
		r_list = [
			{"name": "SANDY 34726", "boxes": 20, "stems": 5000, "status": "Dispatched", "lines": []},
			{"name": "OASIS 34726", "boxes": 5, "stems": 1000, "status": "Dispatched", "lines": []},
		]
		e_list = [
			{"name": "SAL-ORD-1", "boxes": 5, "stems": 1000, "status": "To Deliver and Bill"},
			{"name": "SAL-ORD-2", "boxes": 20, "stems": 5000, "status": "To Deliver and Bill"},
			{"name": "SAL-ORD-3", "boxes": 9, "stems": 900, "status": "To Deliver and Bill"},
		]
		label = {"SAL-ORD-1": "OASIS-04701", "SAL-ORD-2": "SANDY-04809", "SAL-ORD-3": "OTHER-04999"}
		pairs = rc._pair_orders(r_list, e_list, label, {})
		by_r = {p["rosenta"]["name"]: p for p in pairs if p["rosenta"]}
		self.assertEqual(by_r["SANDY 34726"]["erpnext"]["so"], "SAL-ORD-2")
		self.assertEqual(by_r["OASIS 34726"]["erpnext"]["so"], "SAL-ORD-1")
		lone = [p for p in pairs if not p["rosenta"]]
		self.assertEqual([p["erpnext"]["so"] for p in lone], ["SAL-ORD-3"])

	def test_lines_pair_on_variety_and_pack_rate(self):
		r_lines = [
			{
				"line": "AGT 926 YELLOWEEN 340",
				"pack_rate": 340,
				"boxes": 1,
				"stems": 340,
				"length": 60,
				"box_type": "X",
			},
			{
				"line": "AGT 926 PINK 380",
				"pack_rate": 380,
				"boxes": 1,
				"stems": 380,
				"length": 60,
				"box_type": "X",
			},
			{
				"line": "AGT SAMPLE 100",
				"pack_rate": 100,
				"boxes": 1,
				"stems": 100,
				"length": 60,
				"box_type": "X",
			},
		]
		e_lines = [
			{"variety": "Wham", "length": "62cm", "mix_name": "AGT 926 PINK 62CM", "pack_rate": 380, "boxes": 2, "stems": 760},
			{"variety": "Yelloween", "length": "62cm", "mix_name": "", "pack_rate": 340, "boxes": 1, "stems": 340},
			{"variety": "Rare", "length": "50cm", "mix_name": "", "pack_rate": 999, "boxes": 1, "stems": 999},
		]  # fmt: skip
		pairs = rc._pair_lines(r_lines, e_lines)
		both = {p["label"]: p for p in pairs if p["rosenta"] and p["erpnext"]}
		self.assertEqual(both["AGT 926 YELLOWEEN 340"]["erpnext"]["variety"], "Yelloween")  # by variety
		self.assertEqual(both["AGT 926 PINK 380"]["erpnext"]["variety"], "Wham")  # by pack rate and spec name
		self.assertEqual(both["AGT 926 PINK 380"]["erpnext"]["boxes"], 2)  # the real difference stays visible
		self.assertEqual([p["label"] for p in pairs if not p["erpnext"]], ["AGT SAMPLE 100"])
		self.assertEqual([p["label"] for p in pairs if not p["rosenta"]], ["Rare 50cm"])

	def test_rosenta_orders_keep_the_lines_with_pack_rate_and_boxes(self):
		rows = [
			{
				"order_delivery_date": "18/11/2026", "order_id": "o1", "order_name": " AGT  34726 ", "customer": "Agrotropic",
				"order_status": "Dispatched", "line": "AGT 926 PINK 380", "grade": 60, "pack_rate": 380,
				"box_type": "Cargolite", "stems_confirmed": 760, "stems_staged": 380, "stems_loaded": 0,
				"teams": '[{"team_name":"JMF","box_count":"2"}]',
			},
			{"order_delivery_date": "19/11/2026", "order_id": "o2", "customer": "Other"},
		]  # fmt: skip
		out = rc._rosenta_orders(rows, "18/11/2026")
		self.assertEqual(list(out), ["o1"])
		o = out["o1"]
		self.assertEqual((o["name"], o["boxes"], o["stems"], o["staged"]), ("AGT  34726", 2, 760, 380))
		self.assertEqual(
			o["lines"],
			[
				{
					"line": "AGT 926 PINK 380",
					"length": 60,
					"box_type": "Cargolite",
					"boxes": 2,
					"pack_rate": 380,
					"stems": 760,
					"price": None,
					"value": None,
				}
			],
		)

	def test_rosenta_customer_boxes_sum_the_teams(self):
		ops = {
			"Eldama": {"o1": {"s1": {"customer": "Oasis", "boxes_packed": 3, "boxes_staged": 3, "boxes_loaded": 1}}},
			"JMF": {"o1": {"s1": {"customer": "Oasis", "boxes_packed": 2, "boxes_staged": 1, "boxes_loaded": 0}}},
		}  # fmt: skip
		self.assertEqual(rc._rosenta_customer_boxes(ops), {"Oasis": {"packed": 5, "staged": 4, "loaded": 1}})
		self.assertEqual(rc._rosenta_customer_boxes(None), {})

	def test_erp_lines_use_primary_items_only(self):
		wf = {
			"sections": [
				{
					"rows": [
						{"sales_order": "SO1", "primary": True, "variety": "Wham", "length": "62cm", "box_type": "Pro",
						 "mix_name": None, "line_boxes": 2, "stems_per_box": 380, "ordered_stems": 760},
						{"sales_order": "SO1", "primary": False, "variety": "Wham", "line_boxes": 0, "stems_per_box": 380, "ordered_stems": 0},
					]
				}
			]
		}  # fmt: skip
		out = rc._erp_lines(wf)
		self.assertEqual(len(out["SO1"]), 1)
		self.assertEqual(
			(out["SO1"][0]["boxes"], out["SO1"][0]["pack_rate"], out["SO1"][0]["stems"]), (2, 380, 760)
		)

	def test_erp_mixed_box_is_one_line_with_summed_pack_rate(self):
		row = lambda v, **k: {  # noqa: E731
			"sales_order": "SO1", "primary": True, "variety": v, "length": "62cm", "box_type": "Pro",
			"mix_name": "ETHAM MIX 1 L6 240", "line_boxes": 2, "stems_per_box": 60, "ordered_stems": 120, **k
		}  # fmt: skip
		wf = {"sections": [{"rows": [row("A"), row("B"), row("C"), row("D")]}]}
		out = rc._erp_lines(wf)["SO1"]
		self.assertEqual(len(out), 1)
		self.assertEqual((out[0]["boxes"], out[0]["pack_rate"], out[0]["stems"]), (2, 240, 480))
		self.assertTrue(out[0]["mixed"])
		self.assertEqual([v["name"] for v in out[0]["varieties"]], ["A", "B", "C", "D"])

	def test_erp_line_value_and_price_per_stem(self):
		row = lambda soi, amount_key=None: {  # noqa: E731
			"soi": soi, "sales_order": "SO1", "primary": True, "variety": "Wham", "length": "62cm",
			"box_type": "Pro", "mix_name": None, "line_boxes": 2, "stems_per_box": 100, "ordered_stems": 200,
		}  # fmt: skip
		wf = {"sections": [{"rows": [row("i1")]}]}
		out = rc._erp_lines(wf, {"i1": 50.0})["SO1"][0]
		self.assertEqual((out["value"], out["price"]), (50.0, 0.25))
		self.assertEqual(rc._erp_lines(wf)["SO1"][0]["value"], 0)  # no amounts: no value, no price crash

	def test_rosenta_order_gets_its_stem_prices_from_the_order_summary_call(self):
		row = lambda line, stems: {  # noqa: E731
			"order_id": "o1", "order_name": "X", "customer": "C", "order_status": "Processing", "line": line,
			"order_delivery_date": "20/11/2026", "stems_confirmed": stems, "pack_rate": 100, "teams": "[]",
		}  # fmt: skip
		rows = [row("A L6 100", 200), row("B L6 100", 100)]
		prices = {("o1", "A L6 100"): [(0.5, "USD")], ("o1", "B L6 100"): [(0.2, "USD")]}
		o = rc._rosenta_orders(rows, "20/11/2026", prices)["o1"]
		self.assertEqual([(l["price"], l["value"]) for l in o["lines"]], [(0.5, 100.0), (0.2, 20.0)])
		self.assertEqual(rc._money(o["value"], o["stems"]), {"value": 120.0, "price": 0.4, "currency": "USD", "by_currency": {"USD": 120.0}})
		no = rc._rosenta_orders(rows, "20/11/2026")["o1"]  # no prices: nothing shown, nothing invented
		self.assertEqual((no["lines"][0]["price"], rc._money(no["value"], 300)["value"]), (None, None))

	def test_add_value_lists_currencies_instead_of_adding_them(self):
		one = {"stems": 200}
		rc._add_value(one, {"USD": 50.0}, 2)
		self.assertEqual((one["value"], one["price"], one["currency"], one["delivery_notes"]), (50.0, 0.25, "USD", 2))
		many = {"stems": 200}
		rc._add_value(many, {"USD": 50.0, "EUR": 40.0}, 0)
		self.assertEqual((many["value"], many["price"], many["currency"]), (None, None, "EUR + USD"))

	def test_rosenta_teams_sum_the_day_and_take_last_packed_from_both_days(self):
		line = lambda **k: {  # noqa: E731
			"boxes_assigned": 2, "boxes_packed": 1, "boxes_staged": 1, "boxes_loaded": 0, "last_box_packed": 100, **k
		}  # fmt: skip
		day = {"Eldama": {"o1": {"s1": line(), "s2": line(boxes_packed=0, last_box_packed=999)}}}
		nxt = {
			"Eldama": {"o2": {"s1": line(last_box_packed=300)}},
			"JMF": {"o3": {"s1": line(last_box_packed=50)}},
		}
		t = rc._rosenta_teams(day, nxt)
		self.assertEqual(t["Eldama"]["assigned"], 4)  # next day's boxes are not counted
		self.assertEqual(t["Eldama"]["packed"], 1)
		self.assertEqual(t["Eldama"]["last_packed"], 300)  # an unpacked line never sets the time
		self.assertEqual(t["JMF"]["assigned"], 0)
		self.assertEqual(t["JMF"]["last_packed"], 50)

	def test_erp_team_totals_use_the_latest_pack_list_of_mapped_teams(self):
		ins = {
			"Eldama A": {"exp_boxes": 10, "packed_boxes": 4, "pack_last": "18:31"},
			"Eldama B": {"exp_boxes": 5, "packed_boxes": 1, "pack_last": "19:02"},
		}
		t = rc._erp_team_totals(ins, ("Eldama A", "Eldama B", "Missing"), "2026-11-18")
		self.assertEqual((t["exp"], t["packed"], t["last"]), (15, 5, "2026-11-18 19:02:00"))
		self.assertIsNone(rc._erp_team_totals(ins, ("Nobody",), "2026-11-18")["last"])

	def test_erp_team_totals_add_staged_and_loaded_boxes_from_orders_workflow(self):
		ins = {
			"Eldama A": {"exp_boxes": 10, "packed_boxes": 4},
			"Eldama B": {"exp_boxes": 5, "packed_boxes": 1},
		}
		wf = {
			"Eldama A": {"staged_boxes": 3, "loaded_boxes": 2},
			"Eldama B": {"staged_boxes": 1, "loaded_boxes": 1},
		}
		t = rc._erp_team_totals(ins, ("Eldama A", "Eldama B"), "2026-11-18", wf)
		self.assertEqual((t["staged"], t["loaded"]), (4, 3))
		self.assertEqual(rc._erp_team_totals(ins, ("Eldama A",), "2026-11-18")["staged"], 0)


class TestRosentaSession(IntegrationTestCase):
	def _session_returning(self, cookie):
		s = MagicMock()
		s.cookies.get.return_value = cookie
		return s

	def test_signs_in_when_the_session_is_expired_stores_it_and_reuses_it(self):
		store = DictStore("expired")
		ops = rc.RosentaOps("a@b.c", "pw", store)

		# refused with the old cookie, accepted with the new one
		def post(url, **kw):
			if "get_packhouse_operations" in url:
				return _reply(DATA if kw["cookies"]["ci_session"] == "fresh" else None)
			return MagicMock()

		with (
			patch.object(rc.requests, "post", side_effect=post) as p,
			patch.object(rc.requests, "Session", return_value=self._session_returning("fresh")) as sess,
		):
			self.assertEqual(ops.operations("L1", "18/11/2026"), DATA)
			self.assertEqual(sess.return_value.post.call_count, 1)  # one sign-in
			self.assertEqual(store.value, "fresh")
			self.assertEqual(ops.operations("L1", "19/11/2026"), DATA)
			self.assertEqual(sess.return_value.post.call_count, 1)  # the stored session was reused
			self.assertEqual(p.call_count, 3)

	def test_a_date_with_nothing_assigned_is_no_teams_not_a_refusal(self):
		ops = rc.RosentaOps("a@b.c", "pw", DictStore("good"))
		with (
			patch.object(rc.requests, "post", return_value=_reply([])),
			patch.object(rc.requests, "Session") as sess,
		):
			self.assertEqual(ops.operations("L1", "20/11/2026"), {})
			sess.assert_not_called()  # an empty answer is not a refused session

	def test_a_valid_stored_session_never_signs_in(self):
		store = DictStore("good")
		ops = rc.RosentaOps("a@b.c", "pw", store)
		with (
			patch.object(rc.requests, "post", return_value=_reply(DATA)),
			patch.object(rc.requests, "Session") as sess,
		):
			ops.operations("L1", "18/11/2026")
			sess.assert_not_called()
		self.assertEqual(store.sets, 0)

	def test_refused_after_signing_in_is_an_error(self):
		ops = rc.RosentaOps("a@b.c", "pw", DictStore(""))
		with (
			patch.object(rc.requests, "post", return_value=_reply(None)),
			patch.object(rc.requests, "Session", return_value=self._session_returning("c")),
		):
			self.assertRaises(frappe.ValidationError, ops.operations, "L1", "18/11/2026")

	def test_a_login_that_gives_no_session_is_an_error(self):
		ops = rc.RosentaOps("a@b.c", "wrong", DictStore(""))
		with (
			patch.object(rc.requests, "post", return_value=_reply(None)),
			patch.object(rc.requests, "Session", return_value=self._session_returning(None)),
		):
			self.assertRaises(frappe.ValidationError, ops.operations, "L1", "18/11/2026")

	def test_no_login_stored_is_an_error_not_a_blind_call(self):
		ops = rc.RosentaOps("", "", DictStore(""))
		with patch.object(rc.requests, "post", return_value=_reply(None)):
			self.assertRaises(frappe.ValidationError, ops.operations, "L1", "18/11/2026")

	def test_operations_merges_locations_and_repeats_a_call_refused_meanwhile(self):
		class Fake:
			def __init__(self):
				self.refuse_once = {("B", "19/11/2026")}
				self.signed = []

			def operations(self, loc, dmy):
				self.signed.append((loc, dmy))
				return {f"team-{loc}-{dmy}": {}}

			def fetch(self, loc, dmy):
				if (loc, dmy) in self.refuse_once:
					self.refuse_once.discard((loc, dmy))
					return None
				return {f"team-{loc}-{dmy}": {}}

		with patch.object(rc, "LOCATIONS", {"Ravine": "A", "Karen": "B"}):
			fake = Fake()
			out = rc._operations(fake, ["18/11/2026", "19/11/2026"])
		self.assertEqual(set(out), {"18/11/2026", "19/11/2026"})
		self.assertEqual(set(out["18/11/2026"]), {"team-A-18/11/2026", "team-B-18/11/2026"})
		self.assertEqual(set(out["19/11/2026"]), {"team-A-19/11/2026", "team-B-19/11/2026"})
		self.assertEqual(fake.signed[0], ("A", "18/11/2026"))  # the first call goes alone
		self.assertIn(("B", "19/11/2026"), fake.signed)  # the refused one was repeated


MONEY_KEYS = {"value", "price", "currency", "by_currency"}


class TestComparisonCall(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_a_guest_may_not_call_it(self):
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, rc.get_rosenta_comparison, "2026-11-18")

	def test_any_signed_in_user_may_call_it(self):
		user = "Guest"
		for u in frappe.get_all("User", filters={"enabled": 1, "name": ["not in", ["Administrator", "Guest"]]}, pluck="name"):
			if "System Manager" not in frappe.get_roles(u):
				user = u
				break
		if user == "Guest":
			self.skipTest("no enabled user without the System Manager role on this site")
		frappe.set_user(user)
		with patch.object(rc, "_compare", return_value={"success": True}) as comp:
			self.assertEqual(rc.get_rosenta_comparison("2026-11-18"), {"success": True})
		comp.assert_called_once()

	def test_whole_call_on_the_local_site_matches_orders_and_workflow(self):
		frappe.set_user("Administrator")
		fake_ops = MagicMock()
		fake_ops.operations.return_value = {}
		fake_ops.fetch.return_value = {}
		with (
			patch.object(rc, "_settings", return_value={"token": "t", "email": "e", "password": "p"}),
			patch.object(rc, "_orders_summary", return_value=[]),
			patch.object(rc, "RosentaOps", return_value=fake_ops),
		):
			out = rc.get_rosenta_comparison("2026-11-18")
		self.assertTrue(out["success"], out.get("error"))
		self.assertEqual(out["check"], [], "ERPNext totals must equal Orders & Workflow")
		self.assertEqual(out["date"], "2026-11-18")
		for key in ("totals", "customers", "teams", "cancel_rosenta", "cancel_erpnext", "drafts"):
			self.assertIn(key, out)
		self.assertEqual(set(out["totals"]), {"rosenta", "erpnext"})
		self.assertEqual(set(out["totals"]["rosenta"]), set(rc._SIDE_KEYS) | MONEY_KEYS)
		for c in out["customers"]:
			self.assertEqual(set(c["rosenta"]), set(rc._SIDE_KEYS) | MONEY_KEYS)
			self.assertEqual(set(c["erpnext"]), set(rc._SIDE_KEYS) | MONEY_KEYS | {"delivery_notes"})

	def test_rosenta_operations_failing_still_returns_everything_else(self):
		frappe.set_user("Administrator")
		with (
			patch.object(rc, "_settings", return_value={"token": "t", "email": "", "password": ""}),
			patch.object(rc, "_orders_summary", return_value=[]),
		):
			out = rc.get_rosenta_comparison("2026-11-18")
		self.assertTrue(out["success"], out.get("error"))
		self.assertIn("login", (out["ops_error"] or "").lower())
		self.assertTrue(all(t["rosenta"] is None for t in out["teams"] if t["team"] in dict(rc.TEAM_MAP)))
		self.assertIsNone(out["totals"]["rosenta"]["boxes_packed"])  # unknown, not zero
