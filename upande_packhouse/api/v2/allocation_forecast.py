# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""The rolling forecast, as a second forecast to confirm against (the Production forecast, the revised one, is the first).

Rolling forecast (the kaitet-group Server Script "Rolling Forecast"): what a greenhouse cut in a week it cuts again one
cut-to-cut cycle later, so the forecast for week W is the ACTUAL harvest of week W minus the cycle, per variety. The
cycle is the variety's Crop Protocol `weeks_between_cuts` (a variety without one has no rolling forecast and falls back
to the revised forecast). It is shared over the week's seven days and split into lengths by the Crop Protocol's grade
mix (`pct` per `length_cm`); a variety with no grade mix uses the revised forecast's own length shares. Nothing is
stored: it moves with every harvest, so it is computed on read.

Both forecasts get the same treatment as before: the safe part is the forecast less the variety's average error this year,
here measured for each forecast against what was actually cut. Confirmations can draw on either: the one with more that is
safe to promise covers (see allocation_playground.expected_detail).
"""

from datetime import date

import frappe
from frappe.utils import flt, getdate, today

ROLLING = "Rolling forecast"
REVISED = "Production forecast"
BASES = (ROLLING, REVISED)


def _memo(key, fn):
	cache = frappe.flags.setdefault("allocation_forecast_memo", {})
	if key not in cache:
		cache[key] = fn()
	return cache[key]


def protocol(variety):
	"""(weeks between cuts or None, {length: share 0-1}) from the variety's Crop Protocol."""

	def load():
		row = frappe.db.get_value("Crop Protocol", {"variety_item": variety}, ["name", "weeks_between_cuts"], as_dict=True)
		if not row or not row.weeks_between_cuts or row.weeks_between_cuts <= 0:
			return None, {}
		mix = {}
		for g in frappe.get_all("Crop Protocol Grade Mix", filters={"parent": row.name}, fields=["length_cm", "pct"]):
			if flt(g.pct) > 0:
				mix["%dcm" % int(g.length_cm)] = flt(g.pct) / 100.0
		return int(row.weeks_between_cuts), mix

	return _memo(("protocol", variety), load)


def weekly_actuals(variety, farms):
	"""{(iso_year, week): {farm: stems actually cut}} for this and last year."""
	farms = tuple(farms or ())

	def load():
		year = getdate(today()).isocalendar()[0]
		cond, params = "", {"v": variety, "y": year - 1}
		if farms:
			cond = " AND wh.custom_farm IN %(farms)s"
			params["farms"] = farms
		out = {}
		for r in frappe.db.sql(
			f"""SELECT w.iso_year AS y, w.week_number AS wk, COALESCE(NULLIF(wh.custom_farm, ''), 'Unknown') AS farm,
			           SUM(w.actual_stems) AS stems
			    FROM `tabProduction Forecast Week` w
			    INNER JOIN `tabProduction Forecast` f ON f.name = w.parent
			    LEFT JOIN `tabWarehouse` wh ON wh.name = f.greenhouse
			    WHERE f.variety = %(v)s AND w.grade = 'all' AND w.iso_year >= %(y)s AND w.actual_stems > 0 {cond}
			    GROUP BY w.iso_year, w.week_number, farm""",
			params,
			as_dict=True,
		):  # nosemgrep: the f-string hole is fixed SQL, values are bound
			out.setdefault((int(r.y), int(r.wk)), {})[r.farm] = flt(r.stems)
		return out

	return _memo(("actuals", variety, farms), load)


def _back(year, week, cycle):
	"""The (iso year, week) `cycle` weeks before."""
	week -= cycle
	while week < 1:
		year -= 1
		week += date(year, 12, 28).isocalendar()[1]
	return year, week


def _finished(year, week):
	"""A week is finished once it is before the current ISO week."""
	cy, cw, _d = getdate(today()).isocalendar()
	return (year, week) < (cy, cw)


def rolling_by_farm(variety, length, cut_day, farms, fallback_share=None):
	"""{farm: stems per day} at this length for the cutting day, or None when the variety has no rolling forecast."""
	cycle, mix = protocol(variety)
	if not cycle:
		return None
	year, week, _wd = getdate(cut_day).isocalendar()
	sy, sw = _back(year, week, cycle)
	if not _finished(sy, sw):
		return None
	src = weekly_actuals(variety, farms).get((sy, sw))
	if not src:
		return {}
	share = mix.get(length or "") if mix else fallback_share
	if not share:
		return {}
	return {f: s / 7.0 * share for f, s in src.items()}


def rolling_error(variety, farms):
	"""(average error, weeks) of the rolling forecast against what was cut, over this year's finished weeks."""
	cycle, _mix = protocol(variety)
	if not cycle:
		return None, 0
	acts = weekly_actuals(variety, farms)
	year = getdate(today()).isocalendar()[0]
	off = actual = n = 0
	for (y, w), by_farm in acts.items():
		if y != year or not _finished(y, w):
			continue
		prev = acts.get(_back(y, w, cycle))
		if not prev:
			continue
		a, r = sum(by_farm.values()), sum(prev.values())
		actual += a
		off += abs(r - a)
		n += 1
	return (min(1.0, off / actual), n) if actual > 0 else (None, 0)
