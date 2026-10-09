# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Regions: the universal Ravine / Karen filter on every v2 page.

    Karen   = Karen farm only
    Ravine  = Kapkolia, Torongo, Simotwo, Chepsito, Kaptumbo

This module is the ONLY place the grouping is defined (the browser mirror is
PH.REGIONS in public/js/packhouse-v2.js; keep the two in step).

A page applies the region by turning it into a farm list and filtering on the
same farm column it already uses for its farm filter (README rule 6):

    farms = region.farms_for(region=args.get("region"), farm=args.get("farm"))
    if farms is not None:
        conditions.append("opl.farm IN %(farms)s")
        params["farms"] = tuple(farms) or ("",)   # empty tuple = match nothing

farms_for() intersects the region with an explicit farm pick, so choosing
"Karen" and then the farm "Torongo" correctly returns nothing.
"""

REGIONS = {
	"Karen": ("Karen",),
	"Ravine": ("Kapkolia", "Torongo", "Simotwo", "Chepsito", "Kaptumbo"),
}


def normalize(region):
	"""Map any spelling ("ravine", "RAVINE", "All", "") to a REGIONS key or None."""
	if not region:
		return None
	key = str(region).strip().lower()
	for name in REGIONS:
		if name.lower() == key:
			return name
	return None  # "all", unknown -> no region restriction


def region_of(farm):
	"""The region a farm belongs to, or None for a farm in neither region."""
	if not farm:
		return None
	f = str(farm).strip().lower()
	for name, farms in REGIONS.items():
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
	return [farm] if region_of(farm) == r else []


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
