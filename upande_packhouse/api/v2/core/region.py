# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Sites: the universal site filter on every v2 page -- one per sales farm.

The sites are Production Settings' Shelf Locations: every enabled Sales Shelf farm is a site, and every other
enabled shelf farm belongs to the sales farm of its Farm Location (Kapkolia: Kapkolia, Simotwo, Torongo,
Kaptumbo; Chepsito: Chepsito; Karen: Karen). The browser gets the same groups from the page context
(PH.REGIONS, filled by packhouse_v2.page_context), so the two never drift.

"Ravine" -- the old name for every site but Karen -- still resolves, for saved links and older callers.

A page applies the site by turning it into a farm list and filtering on the same farm column it already uses
for its farm filter (README rule 6):

    farms = region.farms_for(region=args.get("region"), farm=args.get("farm"))
    if farms is not None:
        conditions.append("opl.farm IN %(farms)s")
        params["farms"] = tuple(farms) or ("",)   # empty tuple = match nothing

farms_for() intersects the site with an explicit farm pick, so choosing "Karen" and then the farm "Torongo"
correctly returns nothing.
"""

from collections.abc import Mapping

import frappe

#: used only when Production Settings names no sales farm
_FALLBACK = {
	"Karen": ("Karen",),
	"Kapkolia": ("Kapkolia", "Torongo", "Simotwo", "Chepsito", "Kaptumbo"),
}
LEGACY = "Ravine"


def _groups():
	"""{sales farm: (its farms)} from Production Settings, read once per request."""
	cached = getattr(frappe.local, "ph_site_groups", None)
	if cached is not None:
		return cached
	ps = frappe.get_cached_doc("Production Settings")
	rows = [r for r in ps.get("shelf_locations") or [] if r.enabled and r.farm]
	sales = [r.farm for r in rows if r.sales_shelf]
	if not sales:
		out = dict(_FALLBACK)
	else:
		location = dict(
			frappe.get_all(
				"Farm",
				filters={"name": ("in", [r.farm for r in rows])},
				fields=["name", "farm_location"],
				as_list=True,
			)
		)
		groups = {farm: [farm] for farm in sales}
		for r in rows:
			if r.farm in groups:
				continue
			home = next(
				(sf for sf in sales if location.get(sf) and location.get(sf) == location.get(r.farm)), None
			)
			if home:
				groups[home].append(r.farm)
		out = {farm: tuple(farms) for farm, farms in groups.items()}
	frappe.local.ph_site_groups = out
	return out


class _Regions(Mapping):
	"""The sites as a read-only mapping; REGIONS["Ravine"] is every site but Karen's farms."""

	def __getitem__(self, key):
		groups = _groups()
		if key in groups:
			return groups[key]
		if str(key).lower() == LEGACY.lower():
			return tuple(f for site, farms in groups.items() if site != "Karen" for f in farms)
		raise KeyError(key)

	def __iter__(self):
		return iter(_groups())

	def __len__(self):
		return len(_groups())


REGIONS = _Regions()


def sites():
	"""{sales farm: [its farms]} for the page context (the browser's PH.REGIONS)."""
	return {site: list(farms) for site, farms in _groups().items()}


def normalize(region):
	"""Map any spelling ("chepsito", "KAREN", the old "Ravine", "All", "") to a site name or None."""
	if not region:
		return None
	key = str(region).strip().lower()
	for name in REGIONS:
		if name.lower() == key:
			return name
	if key == LEGACY.lower():
		return LEGACY
	return None  # "all", unknown -> no region restriction


def region_of(farm):
	"""The region a farm belongs to, or None for a farm in neither region."""
	if not farm:
		return None
	f = str(farm).strip().lower()
	for name, farms in _groups().items():
		if any(x.lower() == f for x in farms):
			return name
	return None


def farms_for(region=None, farm=None):
	"""Farms a query must be restricted to, or None when nothing restricts it.

	- neither set            -> None (no farm condition at all)
	- farm only              -> [farm]
	- region only            -> the region's farms
	- both                   -> [farm] if it is in the region, else [] (match nothing)
	"""
	r = normalize(region)
	farm = (farm or "").strip() or None
	if not r and not farm:
		return None
	if r and not farm:
		return list(REGIONS[r])
	if farm and not r:
		return [farm]
	return [farm] if farm in REGIONS[r] else []


def sql_tuple(farms):
	"""A tuple safe to bind into `IN %(x)s`; never empty (empty matches nothing)."""
	return tuple(farms) if farms else ("",)


#: The ORDER's farm for order-side figures (README rule 6). Needs the Sales Order
#: aliased `so`; the pick list join is optional — pass `opl_alias=None` when the
#: query has no Order Pick List joined.
def order_farm_sql(so_alias="so", opl_alias="opl"):
	parts = [f"NULLIF({so_alias}.farm, '')", f"NULLIF({so_alias}.custom_farm, '')"]
	if opl_alias:
		parts.append(f"NULLIF({opl_alias}.farm, '')")
	return "COALESCE(" + ", ".join(parts) + ")"


ORDER_FARM_SQL = order_farm_sql()
