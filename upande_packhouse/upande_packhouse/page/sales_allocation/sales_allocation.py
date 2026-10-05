import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime, nowdate

from upande_packhouse import stock_movement


def _origin_warehouse(farm, fallback=None):
	"""Where a bucket's stems start: its own farm's Receiving Cold Store, from the
	warehouse mapping. Not the Shelf Item's warehouse -- an early transfer leg could
	leave a remote bucket's shelf row pointing at the hub, and a pick row copied from
	it then sent the transfer's stock move to the wrong store. A pick row keeps this
	as `origin_warehouse` for good; `source_warehouse` starts here and moves to the
	sales farm's store only once the bucket is shelved there (createShelvingEntry)."""
	from upande_packhouse.roses_warehouse_map import source_warehouse_for_farm

	return (source_warehouse_for_farm(farm) if farm else None) or fallback or ""


# Buckets locked by an open (non-Rejected) Discard Request must never count as
# available or be allocated -- unless that row is already discarded, so a reused
# bucket's fresh harvest does not inherit its previous life's hold. Injected into
# the shelf-availability read paths so the allocation page agrees with the SO
# spec-autofill popup (single rule, see availability.reserved_bucket_ids).
DISCARD_EXCLUSION = """
          AND si.bucket_id NOT IN (
              SELECT drb.bucket_id
              FROM `tabDiscard Request Bucket` drb
              INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
              WHERE COALESCE(dr.workflow_state, '') != 'Rejected'
                AND COALESCE(drb.discarded, 0) = 0
                AND COALESCE(drb.bucket_id, '') != ''
          )"""


def _norm_cut_stage(value) -> str:
	"""Cut stage as a comparable key: "1.5-2.0" and "1.5-2" are the same stage,
	as are "1.0" and "1". Each number loses its trailing ".0"; spaces go."""
	parts = str(value or "").replace(" ", "").split("-")
	out = []
	for part in parts:
		try:
			out.append(f"{float(part):g}")
		except ValueError:
			out.append(part.lower())
	return "-".join(out)


# ============================================================
# HELPER: Load Production Settings config once
# Returns: { discard_age, amber_time, farms_by_location, farm_config }
# farm_config: { farm_name: { sales_shelf, max_allocation_age, cooling_hours } }
# farms_by_location: { location_name: [farm_name, ...] }
# cooling_hours: minimum hours since a bucket was shelved (Shelf Item.date_added)
# before it counts as available -- 0 (the default) means no cooling gate.
# ============================================================
def _get_production_config():
	ps = frappe.get_cached_doc("Production Settings")

	discard_age = ps.discard_age or 5
	amber_time = ps.amber_time or 3

	farm_config = {}  # farm -> { sales_shelf, max_allocation_age }
	farms_by_location = {}  # location -> [farm, ...]

	if not ps.shelf_locations:
		return {
			"discard_age": discard_age,
			"amber_time": amber_time,
			"farm_config": {},
			"farms_by_location": {},
		}

	enabled_farms = [row.farm for row in ps.shelf_locations if row.enabled]

	if not enabled_farms:
		return {
			"discard_age": discard_age,
			"amber_time": amber_time,
			"farm_config": {},
			"farms_by_location": {},
		}

	# Build farm_config from shelf_locations
	for row in ps.shelf_locations:
		if row.enabled:
			farm_config[row.farm] = {
				"sales_shelf": int(row.sales_shelf or 0),
				"max_allocation_age": int(row.max_allocation_age or 5),
				# .get(): `Shelf Locations` is an orphan doctype -- no app in the
				# bench ships it (the directory is empty), so its columns vary by
				# site and `cooling_hours` is simply absent on some. Attribute
				# access raised AttributeError and took the whole allocation page
				# down with it.
				"cooling_hours": float(row.get("cooling_hours") or 0),
			}

	# Single query to get location for all enabled farms.
	# NOTE: this reads `farm_location` (Link -> Location, "Farm Location" on the
	# Farm doctype) — NOT `location`, which is a Geolocation field labeled
	# "Farm Boundary" for the farm's map polygon. Reading `location` here used
	# to accidentally "work" wherever it was misused to hold a plain hub name
	# instead of real boundary data; the moment a farm has genuine boundary
	# GeoJSON in that field, it leaked straight into the location picker.
	placeholders = ", ".join(["%s"] * len(enabled_farms))
	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	farm_rows = frappe.db.sql(
		f"""
        SELECT name AS farm, farm_location AS location
        FROM `tabFarm`
        WHERE name IN ({placeholders})
          AND farm_location IS NOT NULL
          AND farm_location != ''
    """,
		enabled_farms,
		as_dict=True,
	)

	for f in farm_rows:
		loc = f.location
		farms_by_location.setdefault(loc, []).append(f.farm)

	return {
		"discard_age": discard_age,
		"amber_time": amber_time,
		"farm_config": farm_config,
		"farms_by_location": farms_by_location,
	}


def _order_sales_farm(opl):
	"""The sales farm an Order Pick List is packed at, which sets where replacements
	may come from (it and _remote_farms_of it): its Sales Order's farm when that is a
	sales-shelf farm -- an OPL's own farm can name another location (Karen orders
	carried Kapkolia), and Karen must never be offered Ravine buckets -- else the
	OPL's farm, else the transfer hub. `opl`: a name or the document."""
	from upande_packhouse.api.transfer_control import transfer_hub

	if isinstance(opl, str):
		opl = (
			frappe.db.get_value("Order Pick List", opl, ["farm", "sales_order"], as_dict=True)
			or frappe._dict()
		)
	so_farm = (
		frappe.db.get_value("Sales Order", opl.get("sales_order"), "farm") if opl.get("sales_order") else None
	)
	if so_farm and _get_production_config()["farm_config"].get(so_farm, {}).get("sales_shelf"):
		return so_farm
	return opl.get("farm") or transfer_hub(required=False)


def _remote_farms_of(farm):
	"""The remote farms trucked to `farm`: same location, not a sales-shelf farm.
	Another location's farms are never offered -- a line allocated across
	locations fails every later allocation's location check."""
	config = _get_production_config()
	for farms in config["farms_by_location"].values():
		if farm in farms:
			return [f for f in farms if f != farm and not config["farm_config"].get(f, {}).get("sales_shelf")]
	return []


# ============================================================
# HELPER: Default delivery day
# ============================================================
@frappe.whitelist()
def default_delivery_date() -> str:
	"""Tomorrow on the server's clock, in the site's time zone (EAT), not the
	browser's -- orders are allocated the day before they ship."""
	return add_days(nowdate(), 1)


# ============================================================
# HELPER: Location Configuration (for UI location buttons)
# ============================================================
@frappe.whitelist()
def get_location_config():
	config = _get_production_config()
	farms_by_location = config["farms_by_location"]
	farm_config = config["farm_config"]

	locations = []
	for loc_name, farms in farms_by_location.items():
		# The sales shelf farm for this location
		sales_farms = [f for f in farms if farm_config.get(f, {}).get("sales_shelf")]
		remote_farms = [f for f in farms if not farm_config.get(f, {}).get("sales_shelf")]

		locations.append(
			{
				"name": loc_name,
				"farms": farms,
				"sales_farms": sales_farms,
				"remote_farms": remote_farms,
				# farm details for the frontend filter checkboxes
				"farm_details": [
					{
						"farm": f,
						"sales_shelf": farm_config.get(f, {}).get("sales_shelf", 0),
						"max_allocation_age": farm_config.get(f, {}).get("max_allocation_age", 5),
					}
					for f in farms
				],
			}
		)

	return {"locations": locations}


# ============================================================
# HELPER: Get confirmed stems for SO items scoped to specific farms
# Returns: { so_item_name: confirmed_stems_total }
# ============================================================
def _get_confirmed_stems_for_farms(sales_order, farm_names):
	"""
	Fetch confirmed stems from the Sales Order's custom_confirmed_stems_table
	and return totals per SO item, filtered to only the specified farms.

	Returns: dict { sales_order_item_name: total_confirmed_stems }
	"""
	if not farm_names:
		return {}, {}

	farm_placeholders = ", ".join(["%s"] * len(farm_names))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	rows = frappe.db.sql(
		f"""
        SELECT
            cs.sales_order_item,
            cs.farm,
            cs.stems
        FROM `tabConfirmed Stems` cs
        WHERE cs.parent = %s
          AND cs.parenttype = 'Sales Order'
          AND cs.parentfield = 'custom_confirmed_stems_table'
          AND cs.farm IN ({farm_placeholders})
          AND cs.stems > 0
    """,
		[sales_order] + list(farm_names),
		as_dict=True,
	)

	# Sum stems per SO item (multiple farms at the same location may have confirmed)
	confirmed_by_item = {}
	confirmed_detail = {}  # item -> [{farm, stems}]
	for r in rows:
		item_name = r["sales_order_item"]
		confirmed_by_item[item_name] = confirmed_by_item.get(item_name, 0) + (r["stems"] or 0)
		confirmed_detail.setdefault(item_name, []).append({"farm": r["farm"], "stems": r["stems"]})

	return confirmed_by_item, confirmed_detail


def _get_all_confirmed_stems(sales_order):
	"""
	Get ALL confirmed stems for a Sales Order (across all farms).
	Returns: { so_item_name: total_confirmed_stems_all_farms }
	"""
	rows = frappe.db.sql(
		"""
        SELECT
            cs.sales_order_item,
            SUM(cs.stems) AS total_stems
        FROM `tabConfirmed Stems` cs
        WHERE cs.parent = %s
          AND cs.parenttype = 'Sales Order'
          AND cs.parentfield = 'custom_confirmed_stems_table'
          AND cs.stems > 0
        GROUP BY cs.sales_order_item
    """,
		[sales_order],
		as_dict=True,
	)

	return {r["sales_order_item"]: r["total_stems"] or 0 for r in rows}


# ============================================================
# PENDING SALES ORDERS
# ============================================================
@frappe.whitelist()
def get_pending_sales_orders(
	start_date: str | None = None,
	end_date: str | None = None,
	delivery_start: str | None = None,
	delivery_end: str | None = None,
	sales_order: str | None = None,
):
	# `sales_order` is the order a deep-link (Sales Order / Order Pick List
	# "Sales Allocation" button) is opening: it is listed whatever the date
	# window says, so the page can always select it.
	#
	# Bound parameters, not interpolation: every one of these four values comes
	# straight off the request, and this endpoint is whitelisted. They used to be
	# f-stringed into the WHERE clause inside quotes, so `start_date=" OR 1=1 --`
	# rewrote the query for any logged-in user.
	date_conditions = ["so.docstatus = 1", "so.status NOT IN ('Completed', 'Closed', 'Cancelled')"]
	params: dict = {}

	if start_date and end_date:
		date_conditions.append("so.transaction_date BETWEEN %(start_date)s AND %(end_date)s")
		params["start_date"], params["end_date"] = start_date, end_date
	elif start_date:
		date_conditions.append("so.transaction_date >= %(start_date)s")
		params["start_date"] = start_date
	elif end_date:
		date_conditions.append("so.transaction_date <= %(end_date)s")
		params["end_date"] = end_date
	elif not (delivery_start or delivery_end):
		# No window at all: fall back to the last week's orders rather than
		# every open order ever. A delivery window alone is enough -- an order
		# posted weeks ahead for tomorrow must still show for tomorrow.
		date_conditions.append("so.transaction_date >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)")

	if delivery_start and delivery_end:
		date_conditions.append("so.delivery_date BETWEEN %(delivery_start)s AND %(delivery_end)s")
		params["delivery_start"], params["delivery_end"] = delivery_start, delivery_end
	elif delivery_start:
		date_conditions.append("so.delivery_date >= %(delivery_start)s")
		params["delivery_start"] = delivery_start
	elif delivery_end:
		date_conditions.append("so.delivery_date <= %(delivery_end)s")
		params["delivery_end"] = delivery_end

	status_conditions = date_conditions[:2]
	window_clause = " AND ".join(date_conditions)
	if sales_order:
		params["sales_order"] = sales_order
		where_clause = "({0}) OR (so.name = %(sales_order)s AND {1})".format(
			window_clause, " AND ".join(status_conditions)
		)
	else:
		where_clause = window_clause

	# nosemgrep: frappe-sql-format-injection -- interpolates a module-level constant, never request data
	sql = f"""
        WITH so_stats AS (
            SELECT
                so.name AS so_name,
                COUNT(soi.name) AS total_items,
                COUNT(CASE WHEN opl.docstatus = 1 THEN 1 END) AS submitted_items,
                SUM(soi.stock_qty) AS ordered_stems,
                -- How far the order is allocated, by STEMS (allocated / ordered).
                -- Stem-based (not submitted-OPL-count) so remote-farm allocations,
                -- which sit in-transit before their OPL is submitted, still register.
                ROUND(
                    LEAST(100, COALESCE(MAX(alloc.allocated), 0) * 100.0
                        / NULLIF(SUM(soi.stock_qty), 0)),
                    1
                ) AS allocation_percentage,
                GROUP_CONCAT(soi.item_code ORDER BY soi.idx SEPARATOR ', ') AS item_codes,
                GROUP_CONCAT(DISTINCT NULLIF(TRIM(soi.custom_length), '') ORDER BY soi.custom_length SEPARATOR ', ') AS lengths,
                GROUP_CONCAT(DISTINCT NULLIF(TRIM(it.item_group), '') SEPARATOR ', ') AS item_groups,
                MAX(CASE WHEN soi.custom_mixed_box = 1 THEN 1 ELSE 0 END) AS has_mixed,
                MAX(CASE WHEN (soi.custom_mixed_box = 0 OR soi.custom_mixed_box IS NULL) THEN 1 ELSE 0 END) AS has_straight
            FROM `tabSales Order` so
            INNER JOIN `tabSales Order Item` soi ON so.name = soi.parent
            LEFT JOIN `tabOrder Pick List` opl ON soi.custom_opl = opl.name
            LEFT JOIN `tabItem` it ON it.name = soi.item_code
            LEFT JOIN (
                SELECT soi2.parent AS so_name, SUM(ba.quantity_allocated) AS allocated
                FROM `tabBucket Allocations` ba
                INNER JOIN `tabSales Order Item` soi2 ON soi2.name = ba.sales_order_item
                WHERE ba.cancelled = 0
                GROUP BY soi2.parent
            ) alloc ON alloc.so_name = so.name
            WHERE {where_clause}
            GROUP BY so.name
        )
        SELECT
            so.name, so.customer, so.transaction_date, so.delivery_date,
            so.custom_order_name, so.grand_total AS total, so.currency,
            so.status, so.custom_priority,
            ss.total_items, ss.submitted_items, ss.allocation_percentage, ss.item_codes,
            ss.lengths, ss.item_groups, ss.has_mixed, ss.has_straight
        FROM `tabSales Order` so
        INNER JOIN so_stats ss ON so.name = ss.so_name
        ORDER BY so.delivery_date ASC, so.transaction_date DESC
    """

	try:
		# nosemgrep: frappe-sql-format-injection -- the only f-string holes are
		# `where_clause`, built above from fixed SQL fragments; all request values
		# travel in `params` as bound parameters.
		return frappe.db.sql(sql, params, as_dict=True)
	except Exception as e:
		frappe.log_error("Pending SOs Error", frappe.get_traceback())
		frappe.throw(_("Error loading sales orders: {0}").format(str(e)))


@frappe.whitelist()
def get_order_allocation_status(sales_order: str | None):
	"""How far ONE order is allocated, by stems -- same allocated/ordered ratio
	get_pending_sales_orders computes for the whole list, scoped to a single
	order so the Sales Order form's Actions button can label itself without
	pulling the entire pending-orders list. Used to pick "Allocate" /
	"Continue Allocating" / "View Allocation"."""
	if not sales_order:
		frappe.throw(_("Sales Order is required"))

	row = frappe.db.sql(
		"""
        SELECT
            SUM(soi.stock_qty) AS ordered_stems,
            COALESCE(MAX(alloc.allocated), 0) AS allocated_stems
        FROM `tabSales Order Item` soi
        LEFT JOIN (
            SELECT soi2.parent AS so_name, SUM(ba.quantity_allocated) AS allocated
            FROM `tabBucket Allocations` ba
            INNER JOIN `tabSales Order Item` soi2 ON soi2.name = ba.sales_order_item
            WHERE ba.cancelled = 0
            GROUP BY soi2.parent
        ) alloc ON alloc.so_name = soi.parent
        WHERE soi.parent = %s
    """,
		sales_order,
		as_dict=True,
	)

	ordered = float((row[0].ordered_stems if row else 0) or 0)
	allocated = float((row[0].allocated_stems if row else 0) or 0)
	pct = round(min(100, allocated * 100.0 / ordered), 1) if ordered > 0 else 0

	if pct <= 0:
		status = "none"
	elif pct >= 100:
		status = "full"
	else:
		status = "partial"

	return {"ordered_stems": ordered, "allocated_stems": allocated, "percentage": pct, "status": status}


@frappe.whitelist()
def get_order_filter_options():
	"""Complete option lists for the order-list Length and Item-group filters.

	Fetched from the masters (Stem Length, and the item groups ever ordered) rather
	than from whatever orders are on screen — so every value is selectable even when
	no currently-loaded order uses it.
	"""
	# All stem lengths from the master, sorted by their numeric (cm) value.
	lengths = [
		r["name"]
		for r in frappe.db.sql(
			"""
        SELECT name FROM `tabStem Length`
        ORDER BY CAST(REGEXP_REPLACE(name, '[^0-9]', '') AS UNSIGNED), name
    """,
			as_dict=True,
		)
	]

	# Item groups of every item that has ever been ordered (keeps the list complete
	# but relevant — no Consumable / Chemical-Mix noise from unrelated groups).
	item_groups = [
		r["item_group"]
		for r in frappe.db.sql(
			"""
        SELECT DISTINCT it.item_group
        FROM `tabItem` it
        INNER JOIN `tabSales Order Item` soi ON soi.item_code = it.name
        WHERE it.item_group IS NOT NULL AND TRIM(it.item_group) <> ''
        ORDER BY it.item_group
    """,
			as_dict=True,
		)
	]

	# Real Packing Teams, bundled into this SAME call rather than fetched
	# independently -- this page already waits on this call before an
	# operator can do anything useful, so putting teams here (instead of a
	# separate frappe.call racing against the operator's first click) makes
	# it structurally impossible for a team <select> to render before the
	# real list has arrived.
	teams = frappe.get_all("Packing Teams", pluck="name", order_by="name asc")

	return {"lengths": lengths, "item_groups": item_groups, "teams": teams}


# ============================================================
# GET SO ITEMS + AVAILABLE BUCKETS
# UPDATED: Exclude in_transit buckets from availability
# ============================================================
@frappe.whitelist()
def get_sales_order_items_with_buckets(
	sales_order: str | None,
	location: str | None = None,
	selected_farms: str | list | dict | None = None,
	filter_headsize: str | list | dict | None = None,
	filter_color: str | list | dict | None = None,
	filter_cut_stage: str | list | dict | None = None,
	bypass_cut_stage: str | int | float | None = None,
):
	if not sales_order:
		frappe.throw(_("Sales Order is required"))

	bypass_cut_stage = cint(bypass_cut_stage)

	if isinstance(selected_farms, str):
		selected_farms = json.loads(selected_farms)

	headsize_list = []
	if filter_headsize:
		if isinstance(filter_headsize, str):
			headsize_list = [h.strip() for h in filter_headsize.split(",") if h.strip()]
		elif isinstance(filter_headsize, list):
			headsize_list = filter_headsize

	color_list = []
	if filter_color:
		if isinstance(filter_color, str):
			color_list = [c.strip() for c in filter_color.split(",") if c.strip()]
		elif isinstance(filter_color, list):
			color_list = filter_color

	cut_stage_list = []
	if filter_cut_stage:
		if isinstance(filter_cut_stage, str):
			cut_stage_list = [s.strip() for s in filter_cut_stage.split(",") if s.strip()]
		elif isinstance(filter_cut_stage, list):
			cut_stage_list = filter_cut_stage

	config = _get_production_config()
	discard_age = config["discard_age"]
	amber_time = config["amber_time"]
	farm_config = config["farm_config"]
	farms_by_location = config["farms_by_location"]

	location_farms = farms_by_location.get(location, []) if location else []

	if not location_farms:
		frappe.throw(_("No enabled farms found for location: {0}").format(location))

	if selected_farms:
		active_farms = [f for f in selected_farms if f in location_farms]
	else:
		active_farms = [f for f in location_farms if farm_config.get(f, {}).get("sales_shelf")]

	if not active_farms:
		active_farms = location_farms

	farm_max_age = {f: farm_config.get(f, {}).get("max_allocation_age", 5) for f in active_farms}

	# Where an in-transit bucket is heading: this location's sales-shelf farm,
	# the same value _allocate_stock_with_buckets_impl decides `needs_transfer`
	# against. NOT `preferred_farm`, which is merely wherever the most
	# exact-length stock happens to sit and can name the bucket's own origin.
	transit_destination = next((f for f in location_farms if farm_config.get(f, {}).get("sales_shelf")), "")

	confirmed_by_item, confirmed_detail = _get_confirmed_stems_for_farms(sales_order, location_farms)

	items = frappe.db.sql(
		"""
        SELECT
            soi.name AS sales_order_item,
            soi.item_code,
            soi.item_name,
            soi.qty,
            soi.conversion_factor,
            soi.uom,
            soi.stock_uom,
            soi.custom_length AS required_length,
            soi.stock_qty AS original_stock_qty,
            soi.custom_ordered_quantity AS original_ordered_qty,
            soi.custom_mixed_box,
            soi.custom_mix_group,
            soi.custom_mix_name,
            soi.custom_mixed_bunch,
            soi.custom_bunch_group,
            soi.custom_line AS specification,
            soi.custom_cut_stage
        FROM `tabSales Order Item` soi
        WHERE soi.parent = %s
        ORDER BY soi.idx
    """,
		[sales_order],
		as_dict=True,
	)

	if not items:
		return []

	# Cut-stage matching is driven entirely by what THIS Sales Order Item
	# itself carries in custom_cut_stage -- not a fresh re-lookup of the
	# spec's own cut_stage, and not a rose-type rule (e.g. "sprays never
	# have one"). custom_cut_stage is populated from the spec at fill time
	# (see spec_autofill._detail_payload) but is the order's own value from
	# then on -- re-deriving it from the spec here could disagree if the
	# spec changed since, or silently apply a filter to a line whose own
	# cut_stage was never set (a mismatch this used to force on Spray Roses
	# lines in particular, since some spray specs do carry a spec cut_stage,
	# but the packhouse's own spray grading step has no cut_stage concept to
	# match it against). No filter at all when custom_cut_stage is blank.

	all_confirmed = _get_all_confirmed_stems(sales_order)

	filtered_items = []
	for item in items:
		so_item_name = item["sales_order_item"]
		my_confirmed = confirmed_by_item.get(so_item_name, 0)
		total_all_confirmed = all_confirmed.get(so_item_name, 0)
		others_confirmed = total_all_confirmed - my_confirmed
		ordered_qty = item["original_ordered_qty"] or item["original_stock_qty"] or 0

		if my_confirmed > 0:
			item["pending_stock_qty"] = my_confirmed
			item["confirmed_stems"] = my_confirmed
			item["confirmed_detail"] = confirmed_detail.get(so_item_name, [])
			item["total_all_confirmed"] = total_all_confirmed
			item["others_confirmed"] = others_confirmed
			item["balance_available"] = max(0, ordered_qty - total_all_confirmed)
			filtered_items.append(item)
		elif not all_confirmed:
			item["pending_stock_qty"] = ordered_qty
			item["confirmed_stems"] = 0
			item["confirmed_detail"] = []
			item["total_all_confirmed"] = 0
			item["others_confirmed"] = 0
			item["balance_available"] = ordered_qty
			filtered_items.append(item)

	items = filtered_items

	if not items:
		return []

	item_codes = list({i["item_code"] for i in items})

	ic_placeholders = ", ".join(["%s"] * len(item_codes))
	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	item_metadata = frappe.db.sql(
		f"""
        SELECT
            name AS item_code,
            custom_headsize_cm AS headsize,
            custom_color AS color
        FROM `tabItem`
        WHERE name IN ({ic_placeholders})
    """,
		item_codes,
		as_dict=True,
	)

	metadata_map = {row["item_code"]: row for row in item_metadata}

	if headsize_list or color_list:
		filtered = []
		for item in items:
			meta = metadata_map.get(item["item_code"], {})
			item_headsize = str(meta.get("headsize", "")).strip()
			item_color = str(meta.get("color", "")).strip()

			headsize_match = not headsize_list or item_headsize in headsize_list
			color_match = not color_list or item_color in color_list

			if headsize_match and color_match:
				filtered.append(item)

		items = filtered

		if not items:
			return []

		item_codes = list({i["item_code"] for i in items})

	ic_placeholders = ", ".join(["%s"] * len(item_codes))
	farm_placeholders = ", ".join(["%s"] * len(active_farms))

	# ── In-transit buckets STAY allocatable ──────────────────────────────────
	# A bucket on its way to the sales shelf still physically holds its stems,
	# and they will all be at the destination when it lands, so a second order
	# may legitimately claim from it. It is surfaced with in_transit = 1 so the
	# page can badge it rather than hide it. Availability here is the shelf
	# row's own count minus what is already allocated -- it never reads
	# BucketAllocationStatus.available_quantity -- so nothing else needs to move.
	# Submission is still gated: opl_submit_blockers holds the OPL until the
	# bucket actually arrives.
	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	buckets = frappe.db.sql(
		f"""
        SELECT
            si.bucket_id,
            si.variety AS item_code,
            si.stem_length,
            COALESCE(si.stem_qty, 0) AS total_qty,
            si.warehouse,
            COALESCE(si.harvest_date, si.date_added) AS harvest_date,
            s.name AS shelf_location,
            s.farm AS shelf_farm,
            DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) AS age_days,
            TIMESTAMPDIFF(HOUR, si.date_added, %s) AS hours_since_shelved,
            COALESCE(bas.allocated_quantity, 0) AS allocated_qty,
            GREATEST(0, COALESCE(si.stem_qty, 0) - COALESCE(bas.allocated_quantity, 0)) AS available_qty,
            COALESCE(si.cut_stage, '') AS cut_stage,
            COALESCE(bas.in_transit, 0) AS in_transit
        FROM (
            -- One row per (bucket, variety, length, shelf). Shelving can write the
            -- same bucket's stems as several rows (one per receiving row), and a
            -- per-row LEFT JOIN to Bucket Allocation Status would subtract the
            -- whole allocation from EACH row -- which is how the page showed
            -- -40 / -30 for a 70-stem bucket fully allocated.
            SELECT bucket_id, variety, stem_length, parent,
                   SUM(COALESCE(stem_qty, 0)) AS stem_qty,
                   MIN(date_added) AS date_added,
                   MIN(harvest_date) AS harvest_date,
                   MIN(warehouse) AS warehouse,
                   MIN(cut_stage) AS cut_stage
            FROM `tabShelf Item`
            GROUP BY bucket_id, variety, stem_length, parent
        ) si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        LEFT JOIN `tabBucket Allocation Status` bas
            ON bas.bucket_id = si.bucket_id
            AND bas.item_code = si.variety
            AND COALESCE(bas.stem_length, '') = COALESCE(si.stem_length, '')
        WHERE s.farm IN ({farm_placeholders})
          AND si.variety IN ({ic_placeholders})
          AND DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) < %s
          {DISCARD_EXCLUSION}
    """,
		[now_datetime()] + active_farms + item_codes + [discard_age],
		as_dict=True,
	)

	buckets = [b for b in buckets if b["age_days"] <= farm_max_age.get(b["shelf_farm"], 5)]

	# ── Exclude buckets still cooling: fewer hours on the shelf than this
	# farm's configured cooling_hours (Shelf Locations, Production Settings).
	# Measured from Shelf Item.date_added (when it was scanned onto the
	# shelf) specifically -- NOT harvest_date/age_days, which the ceiling
	# check above already uses for a different purpose (how long it's been
	# since harvest, not how long it's been in the cold store). 0 hours
	# (the default) means this farm has no cooling requirement configured.
	buckets = [
		b
		for b in buckets
		if (b["hours_since_shelved"] or 0) >= farm_config.get(b["shelf_farm"], {}).get("cooling_hours", 0)
	]

	# ── Apply cut_stage filter to buckets ──
	if cut_stage_list:
		wanted_stages = {_norm_cut_stage(s) for s in cut_stage_list}
		buckets = [b for b in buckets if _norm_cut_stage(b.get("cut_stage")) in wanted_stages]

	so_item_names = [i["sales_order_item"] for i in items]
	si_placeholders = ", ".join(["%s"] * len(so_item_names))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	allocated_per_item = frappe.db.sql(
		f"""
        SELECT
            ba.sales_order_item,
            SUM(ba.quantity_allocated) AS total_allocated
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item IN ({si_placeholders})
          AND ba.cancelled = 0
        GROUP BY ba.sales_order_item
    """,
		so_item_names,
		as_dict=True,
	)

	allocated_dict = {row["sales_order_item"]: row["total_allocated"] or 0 for row in allocated_per_item}

	per_bucket_per_item = {}
	if buckets:
		bucket_ids = list({b["bucket_id"] for b in buckets})
		bid_placeholders = ", ".join(["%s"] * len(bucket_ids))

		# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
		result = frappe.db.sql(
			f"""
            SELECT
                bas.bucket_id,
                bas.item_code,
                COALESCE(bas.stem_length, '') AS stem_length,
                ba.sales_order_item,
                SUM(ba.quantity_allocated) AS allocated_to_this_item
            FROM `tabBucket Allocations` ba
            INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
            WHERE ba.sales_order_item IN ({si_placeholders})
              AND bas.bucket_id IN ({bid_placeholders})
              AND ba.cancelled = 0
            GROUP BY bas.bucket_id, bas.item_code, COALESCE(bas.stem_length, ''), ba.sales_order_item
        """,
			so_item_names + bucket_ids,
			as_dict=True,
		)

		for row in result:
			# Keyed like BAS itself -- (bucket, variety, length) -- so a bucket
			# carrying several varieties/lengths reports each row's own share.
			key = (row["bucket_id"], row["item_code"], row["stem_length"], row["sales_order_item"])
			per_bucket_per_item[key] = row["allocated_to_this_item"] or 0

	buckets_by_item = {}
	for b in buckets:
		buckets_by_item.setdefault(b["item_code"], []).append(b)

	for item in items:
		meta = metadata_map.get(item["item_code"], {})
		item["headsize"] = meta.get("headsize", "")
		item["color"] = meta.get("color", "")
		item["spec_cut_stage"] = item.get("custom_cut_stage") or None

		item_buckets = buckets_by_item.get(item["item_code"], [])
		req_cm = _parse_cm(item["required_length"])

		exact = []
		downgrade = []

		for b in item_buckets:
			# Spec-driven lines only ever see buckets at the spec's own cut
			# stage — no manual range filter needed or offered for these.
			# Bypassed entirely when the allocator has explicitly checked
			# "bypass cut stage" on the page, so mismatched buckets become
			# selectable instead of being silently hidden.
			if (
				not bypass_cut_stage
				and item["spec_cut_stage"]
				and _norm_cut_stage(b.get("cut_stage")) != _norm_cut_stage(item["spec_cut_stage"])
			):
				continue

			b_cm = _parse_cm(b["stem_length"])
			is_sales_shelf = farm_config.get(b["shelf_farm"], {}).get("sales_shelf", 0)

			if b_cm == req_cm and b_cm > 0:
				status = "exact"
			elif b_cm > req_cm and b_cm > 0:
				status = "downgrade"
			else:
				continue

			allocated_here = per_bucket_per_item.get(
				(b["bucket_id"], b["item_code"], b["stem_length"] or "", item["sales_order_item"]), 0
			)

			# Fully allocated elsewhere: nothing to allocate and nothing to
			# unallocate from this line, so keep it off the list.
			if (b["available_qty"] or 0) <= 0 and not allocated_here:
				continue

			entry = {
				"bucket_id": b["bucket_id"],
				"stem_length": b["stem_length"],
				"total_qty": b["total_qty"],
				"allocated_qty": b["allocated_qty"],
				"available_qty": b["available_qty"],
				"allocated_to_this_item": allocated_here,
				"age_days": b["age_days"],
				"hours_since_shelved": b["hours_since_shelved"],
				"shelf_location": b["shelf_location"],
				"shelf_farm": b["shelf_farm"],
				"harvest_date": b["harvest_date"],
				"warehouse": b["warehouse"],
				"cut_stage": b.get("cut_stage", ""),
				"length_status": status,
				"is_sales_shelf": is_sales_shelf,
				"awaiting_transfer": 0 if is_sales_shelf else 1,
				# Already allocated to someone and physically on its way to the
				# sales shelf -- allocatable, but shown as in transit.
				"in_transit": cint(b.get("in_transit")),
				"transit_to": transit_destination if cint(b.get("in_transit")) else "",
				"downgrade_approval": (
					"amber_expired" if (b["age_days"] or 0) >= amber_time else "requires_approval"
				)
				if status == "downgrade"
				else "",
			}

			if status == "exact":
				exact.append(entry)
			else:
				downgrade.append(entry)

		farm_exact_qty = {}
		for b in exact:
			farm_exact_qty[b["shelf_farm"]] = farm_exact_qty.get(b["shelf_farm"], 0) + (
				b["available_qty"] or 0
			)

		preferred_farm = max(farm_exact_qty, key=farm_exact_qty.get) if farm_exact_qty else None

		def sort_key(b):
			is_preferred = 0 if b["shelf_farm"] == preferred_farm else 1
			return (is_preferred, b["age_days"] * -1)

		exact.sort(key=sort_key)
		downgrade.sort(key=sort_key)

		compatible = exact + downgrade

		item["batches"] = compatible
		item["total_available_qty"] = sum(b["available_qty"] for b in compatible)
		item["total_allocated_qty"] = allocated_dict.get(item["sales_order_item"], 0)
		item["has_sufficient_stock"] = item["total_available_qty"] >= item["pending_stock_qty"]
		item["preferred_farm"] = preferred_farm
		item["amber_time"] = amber_time

	if items and location and active_farms:
		_attach_incoming_stems(items, location, active_farms)
	else:
		for item in items:
			item["incoming_exact_stems"] = 0

	return items


# ============================================================
# BUCKET VISIBILITY DIAGNOSTICS
# "No compatible buckets found" is a dead end for the user — buckets can be
# physically on a coldstore shelf and still be invisible to allocation for any
# of: pending discard, in transit, too old (discard age or the farm's own
# max-allocation-age), too short to downgrade, a cut-stage filter, or sitting on
# a farm the current location isn't showing. This endpoint runs the SAME
# eligibility rules as get_sales_order_items_with_buckets but keeps every
# bucket instead of silently dropping the ineligible ones, tagging each with
# why it's (or isn't) showing.
# ============================================================
@frappe.whitelist()
def get_bucket_visibility_diagnostics(
	sales_order_item: str | None,
	location: str | None = None,
	selected_farms: str | list | dict | None = None,
	filter_cut_stage: str | list | dict | None = None,
	bypass_cut_stage: str | int | float | None = None,
):
	if not sales_order_item:
		frappe.throw(_("Sales Order Item is required"))

	bypass_cut_stage = cint(bypass_cut_stage)

	if isinstance(selected_farms, str):
		selected_farms = json.loads(selected_farms) if selected_farms else []
	selected_farms = selected_farms or []

	cut_stage_list = []
	if filter_cut_stage:
		if isinstance(filter_cut_stage, str):
			cut_stage_list = [s.strip() for s in filter_cut_stage.split(",") if s.strip()]
		elif isinstance(filter_cut_stage, list):
			cut_stage_list = filter_cut_stage

	soi = frappe.db.get_value(
		"Sales Order Item",
		sales_order_item,
		["item_code", "custom_length", "custom_line", "custom_cut_stage"],
		as_dict=True,
	)
	if not soi:
		frappe.throw(_("Sales Order Item not found: {0}").format(sales_order_item))

	item_code = soi.item_code
	required_length = soi.custom_length
	req_cm = _parse_cm(required_length)

	# Driven by what THIS line itself carries in custom_cut_stage -- not a
	# fresh spec re-lookup, and not a rose-type rule -- matching
	# get_sales_order_items_with_buckets. No filter at all when it's blank
	# (e.g. most Spray Roses lines, which have no cut-stage concept on the
	# packhouse side even when the spec happens to carry one).
	spec_cut_stage = soi.custom_cut_stage or None
	if spec_cut_stage and not bypass_cut_stage:
		cut_stage_list = [spec_cut_stage]
	if bypass_cut_stage:
		cut_stage_list = []

	config = _get_production_config()
	discard_age = config["discard_age"]
	amber_time = config["amber_time"]
	farm_config = config["farm_config"]
	farms_by_location = config["farms_by_location"]

	# Every farm belonging to this location (sales-shelf AND remote) — the full
	# universe a bucket could plausibly be on and still be "for this order".
	location_farms = farms_by_location.get(location, []) if location else []
	if selected_farms:
		active_farms = [f for f in selected_farms if f in location_farms]
	else:
		active_farms = [f for f in location_farms if farm_config.get(f, {}).get("sales_shelf")]
	if not active_farms:
		active_farms = location_farms

	if not location_farms:
		# No location context to scope the scan to — fall back to whatever farms
		# have ever held this variety, so the diagnostic still says something.
		location_farms = [
			r["farm"]
			# One SQL statement split across lines on purpose, not a missing comma.
			for r in frappe.db.sql(
				# nosemgrep: string-concat-in-list
				"SELECT DISTINCT s.farm AS farm FROM `tabShelf` s "
				"INNER JOIN `tabShelf Item` si ON si.parent = s.name WHERE si.variety = %s",
				[item_code],
				as_dict=True,
			)
		]

	if not location_farms:
		return {
			"success": True,
			"item_code": item_code,
			"required_length": required_length,
			"spec_cut_stage": spec_cut_stage,
			"total_buckets": 0,
			"reasons": [],
		}

	farm_ph = ", ".join(["%s"] * len(location_farms))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	rows = frappe.db.sql(
		f"""
        SELECT
            si.bucket_id,
            si.stem_length,
            COALESCE(si.stem_qty, 0) AS stems,
            s.farm AS shelf_farm,
            s.name AS shelf_location,
            DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) AS age_days,
            TIMESTAMPDIFF(HOUR, si.date_added, %s) AS hours_since_shelved,
            COALESCE(si.cut_stage, '') AS cut_stage,
            COALESCE(bas.allocated_quantity, 0) AS allocated_qty,
            COALESCE(bas.in_transit, 0) AS in_transit,
            (
                SELECT dr.name FROM `tabDiscard Request Bucket` drb
                INNER JOIN `tabDiscard Request` dr ON dr.name = drb.parent
                WHERE drb.bucket_id = si.bucket_id
                  AND COALESCE(dr.workflow_state, '') != 'Rejected'
                  AND COALESCE(drb.discarded, 0) = 0
                ORDER BY dr.creation DESC LIMIT 1
            ) AS discard_request
        FROM (
            -- One row per (bucket, variety, length, shelf). Shelving can write the
            -- same bucket's stems as several rows (one per receiving row), and a
            -- per-row LEFT JOIN to Bucket Allocation Status would subtract the
            -- whole allocation from EACH row -- which is how the page showed
            -- -40 / -30 for a 70-stem bucket fully allocated.
            SELECT bucket_id, variety, stem_length, parent,
                   SUM(COALESCE(stem_qty, 0)) AS stem_qty,
                   MIN(date_added) AS date_added,
                   MIN(harvest_date) AS harvest_date,
                   MIN(warehouse) AS warehouse,
                   MIN(cut_stage) AS cut_stage
            FROM `tabShelf Item`
            GROUP BY bucket_id, variety, stem_length, parent
        ) si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        LEFT JOIN `tabBucket Allocation Status` bas
            ON bas.bucket_id = si.bucket_id AND bas.item_code = si.variety
            AND COALESCE(bas.stem_length, '') = COALESCE(si.stem_length, '')
        WHERE si.variety = %s
          AND s.farm IN ({farm_ph})
          AND COALESCE(si.stem_qty, 0) > 0
    """,
		[now_datetime(), item_code] + location_farms,
		as_dict=True,
	)

	# Priority order: the most actionable / most likely cause wins when a bucket
	# has more than one issue, so the summary doesn't double-count buckets.
	REASON_META = [
		("pending_discard", "Pending discard request"),
		("remote_farm", "On a farm not currently selected"),
		("cooling", "Still cooling since shelving"),
		("past_discard_age", "Past the discard-age threshold"),
		("past_max_age", "Past this farm's allocation-age limit"),
		("too_short", "Shorter than the order needs"),
		("wrong_cut_stage", "Doesn't match the active cut-stage filter"),
		("fully_allocated", "Already fully allocated"),
		("eligible", "Available to allocate"),
	]
	buckets_by_reason = {code: [] for code, _l in REASON_META}

	wanted_stages = {_norm_cut_stage(s) for s in cut_stage_list}
	for b in rows:
		b_cm = _parse_cm(b["stem_length"])
		is_sales_shelf = farm_config.get(b["shelf_farm"], {}).get("sales_shelf", 0)
		farm_max_age = farm_config.get(b["shelf_farm"], {}).get("max_allocation_age", 5)
		available_qty = max(0, (b["stems"] or 0) - (b["allocated_qty"] or 0))
		age_days = b["age_days"] or 0

		reason = None
		detail = None
		if b["discard_request"]:
			reason = "pending_discard"
			detail = _("On discard request {0} — not available until it's resolved").format(
				b["discard_request"]
			)
		elif not is_sales_shelf and b["shelf_farm"] not in active_farms:
			reason = "remote_farm"
			detail = _("On {0}, which isn't one of the farms currently selected").format(
				b["shelf_farm"] or "?"
			)
		elif farm_config.get(b["shelf_farm"], {}).get("cooling_hours", 0) and (
			b["hours_since_shelved"] or 0
		) < farm_config.get(b["shelf_farm"], {}).get("cooling_hours", 0):
			reason = "cooling"
			cooling_hours = farm_config.get(b["shelf_farm"], {}).get("cooling_hours", 0)
			hours_left = cooling_hours - (b["hours_since_shelved"] or 0)
			detail = _("Shelved {0}h ago — {1} requires {2}h cooling, {3}h left").format(
				b["hours_since_shelved"] or 0, b["shelf_farm"] or "this farm", cooling_hours, hours_left
			)
		elif age_days >= discard_age:
			reason = "past_discard_age"
			detail = _("{0} days old — past the {1}-day discard-age limit").format(age_days, discard_age)
		elif age_days > farm_max_age:
			reason = "past_max_age"
			detail = _("{0} days old — past {1}'s {2}-day allocation-age limit").format(
				age_days, b["shelf_farm"] or "this farm", farm_max_age
			)
		elif b_cm <= 0 or b_cm < req_cm:
			reason = "too_short"
			detail = _("{0} stem — the order needs {1}").format(
				b["stem_length"] or "?", required_length or "?"
			)
		elif cut_stage_list and _norm_cut_stage(b["cut_stage"]) not in wanted_stages:
			reason = "wrong_cut_stage"
			if spec_cut_stage:
				detail = _("Cut stage {0} — the order spec requires {1}").format(
					b["cut_stage"] or "-", spec_cut_stage
				)
			else:
				detail = _("Cut stage {0} isn't in the active cut-stage filter").format(b["cut_stage"] or "-")
		elif available_qty <= 0:
			reason = "fully_allocated"
			detail = _("All {0} stems on this bucket are already allocated").format(b["stems"] or 0)
		else:
			reason = "eligible"
			detail = _("Available to allocate")

		buckets_by_reason[reason].append(
			{
				"bucket_id": b["bucket_id"],
				"stem_length": b["stem_length"],
				"stems": b["stems"],
				"available_qty": max(0, available_qty),
				"farm": b["shelf_farm"],
				"shelf": b["shelf_location"],
				"age_days": b["age_days"],
				"hours_since_shelved": b["hours_since_shelved"],
				"cut_stage": b["cut_stage"],
				"discard_request": b["discard_request"],
				"detail": detail,
			}
		)

	reasons = []
	for code, label in REASON_META:
		bucket_list = buckets_by_reason[code]
		if not bucket_list:
			continue
		reasons.append(
			{
				"code": code,
				"label": label,
				"count": len(bucket_list),
				"stems": sum(x["stems"] or 0 for x in bucket_list),
				"buckets": bucket_list,
			}
		)

	return {
		"success": True,
		"item_code": item_code,
		"required_length": required_length,
		"spec_cut_stage": spec_cut_stage,
		"total_buckets": len(rows),
		"reasons": reasons,
	}


def _parse_cm(length_str):
	"""Parse stem length string like '60cm' -> 60. Returns 0 on failure."""
	if not length_str:
		return 0
	try:
		return int(str(length_str).lower().replace("cm", "").strip())
	except:
		return 0


def _attach_incoming_stems(items, location, active_farms):
	"""Attach incoming (received but not yet shelved) stem counts to each item."""
	item_length_map = {}
	for item in items:
		if item.get("required_length") and item.get("item_code"):
			key = (item["item_code"], item["required_length"])
			item_length_map.setdefault(key, []).append(item)

	if not item_length_map:
		for item in items:
			item["incoming_exact_stems"] = 0
		return

	item_codes_for_incoming = list({k[0] for k in item_length_map})
	lengths_for_incoming = list({k[1] for k in item_length_map})

	ic_placeholders = ", ".join(["%s"] * len(item_codes_for_incoming))
	ln_placeholders = ", ".join(["%s"] * len(lengths_for_incoming))
	farm_ph = ", ".join(["%s"] * len(active_farms))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	unshelved = frappe.db.sql(
		f"""
        SELECT
            se.custom_bucket_id AS bucket_id,
            sei.item_code,
            se.custom_stem_length AS stem_length,
            sei.qty
        FROM `tabStock Entry` se
        INNER JOIN `tabStock Entry Detail` sei ON se.name = sei.parent
        WHERE se.stock_entry_type IN ('Receiving', 'Late Receipt')
          AND se.docstatus = 1
          AND se.farm IN ({farm_ph})
          AND se.posting_date >= DATE_SUB(CURDATE(), INTERVAL 5 DAY)
          AND sei.item_code IN ({ic_placeholders})
          AND se.custom_stem_length IN ({ln_placeholders})
          AND se.custom_bucket_id IS NOT NULL
          AND se.custom_bucket_id != ''
          -- Unshelved means the bucket has NEITHER a live shelf row NOR any
          -- record of having been shelved. Both halves earn their place: the
          -- Shelf Item alone would re-count a bucket that was shelved and then
          -- issued (issuing removes/empties that row), while the Shelving Log
          -- alone would miss a bucket sitting on a shelf whose log write failed
          -- -- that write is best-effort. The log test is the same one
          -- api/stem_movement.py uses for its received-vs-shelved figures.
          AND NOT EXISTS (
              SELECT 1 FROM `tabShelf Item` si
              WHERE si.bucket_id = se.custom_bucket_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM `tabShelving Log` sl
              WHERE sl.bucket_id = se.custom_bucket_id
                AND sl.shelved_on >= se.posting_date
          )
    """,
		active_farms + item_codes_for_incoming + lengths_for_incoming,
		as_dict=True,
	)

	incoming_map = {}
	if unshelved:
		bucket_ids = list({r["bucket_id"] for r in unshelved})
		bid_placeholders = ", ".join(["%s"] * len(bucket_ids))

		# Incoming stock is only worth advertising if nobody has claimed it yet.
		# This used to look for `custom_issued_to` on a HARVESTING entry, but
		# allocation stamps that field on the sale transfer it posts
		# (stock_movement.move_allocation_to_sold), not on the harvest -- so the
		# check almost never matched and already-allocated buckets kept being
		# counted as available incoming stems. Ask the allocation records
		# directly instead, and take the stamp from any entry type.
		# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
		issued_rows = frappe.db.sql(
			f"""
            SELECT DISTINCT se.custom_bucket_id
            FROM `tabStock Entry` se
            WHERE se.docstatus = 1
              AND se.custom_bucket_id IN ({bid_placeholders})
              AND COALESCE(se.custom_issued_to, '') != ''
            UNION
            SELECT DISTINCT bas.bucket_id
            FROM `tabBucket Allocation Status` bas
            WHERE bas.bucket_id IN ({bid_placeholders})
              AND COALESCE(bas.allocated_quantity, 0) > 0
        """,
			bucket_ids + bucket_ids,
		)

		issued_set = {r[0] for r in issued_rows}

		for r in unshelved:
			if r["bucket_id"] not in issued_set:
				key = (r["item_code"], r["stem_length"])
				incoming_map[key] = incoming_map.get(key, 0) + (r["qty"] or 0)

	for item in items:
		key = (item.get("item_code"), item.get("required_length"))
		item["incoming_exact_stems"] = incoming_map.get(key, 0)


# ============================================================
# NEW: Get available headsize and color options for current SO
# ============================================================
@frappe.whitelist()
def get_available_filters(
	sales_order: str | None, location: str | None = None, selected_farms: str | list | dict | None = None
):
	"""
	Returns available headsize and color values for items in the sales order
	that have confirmed stems at the selected location.
	"""
	if not sales_order or not location:
		return {"headsizes": [], "colors": []}

	# Parse selected_farms
	if isinstance(selected_farms, str):
		selected_farms = json.loads(selected_farms)

	config = _get_production_config()
	farms_by_location = config["farms_by_location"]
	location_farms = farms_by_location.get(location, [])

	if not location_farms:
		return {"headsizes": [], "colors": []}

	# Get confirmed items for this location
	confirmed_by_item = _get_confirmed_stems_for_farms(sales_order, location_farms)[0]

	# Fetch SO items
	items = frappe.db.sql(
		"""
        SELECT DISTINCT soi.item_code
        FROM `tabSales Order Item` soi
        WHERE soi.parent = %s
    """,
		[sales_order],
		as_dict=True,
	)

	# Filter to only confirmed items
	all_confirmed = _get_all_confirmed_stems(sales_order)

	# Get item codes with confirmed stems or no confirmations exist
	confirmed_item_codes = []
	for item in items:
		# Check if this specific item has confirmed stems at this location
		# or if no confirmations exist at all
		has_local_confirmed = any(
			so_item
			for so_item in confirmed_by_item.keys()
			if frappe.db.get_value("Sales Order Item", so_item, "item_code") == item["item_code"]
		)

		if has_local_confirmed or not all_confirmed:
			confirmed_item_codes.append(item["item_code"])

	if not confirmed_item_codes:
		return {"headsizes": [], "colors": []}

	# Fetch unique headsize and color values
	ic_placeholders = ", ".join(["%s"] * len(confirmed_item_codes))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	metadata = frappe.db.sql(
		f"""
        SELECT DISTINCT
            custom_headsize_cm AS headsize,
            custom_color AS color
        FROM `tabItem`
        WHERE name IN ({ic_placeholders})
          AND (custom_headsize_cm IS NOT NULL OR custom_color IS NOT NULL)
    """,
		confirmed_item_codes,
		as_dict=True,
	)

	headsizes = sorted(list({str(row["headsize"]).strip() for row in metadata if row["headsize"]}))
	colors = sorted(list({str(row["color"]).strip() for row in metadata if row["color"]}))

	return {"headsizes": headsizes, "colors": colors}


def recompute_bas_quantities(bas, shelf_qty=None):
	"""Re-derive a Bucket Allocation Status' quantities from its own rows.

	`allocated_quantity` counts only OUTSTANDING allocations -- not cancelled,
	not yet issued. An issued row's stems have physically left the bucket, and
	the issue decrements `Shelf Item.stem_qty` at the same moment, so counting
	it here too would subtract it twice: every availability read is
	`stem_qty - allocated_quantity` (see the bucket query above, availability.py
	and get_substitute_varieties), and a 100-stem bucket with 20 issued would
	report 60 free instead of 80.

	`shelf_qty` re-syncs `total_quantity`, which is otherwise a snapshot taken
	when the row was created. Pass the live `Shelf Item.stem_qty` whenever the
	bucket's physical contents change, or `available_quantity` -- which the
	over-allocation guard reads -- drifts above what is actually on the shelf.
	"""
	if shelf_qty is not None:
		bas.total_quantity = flt(shelf_qty)

	outstanding = [r for r in bas.bucket_allocations if not r.cancelled and not r.issued]
	bas.allocated_quantity = sum(flt(r.quantity_allocated) for r in outstanding)
	# Floored at 0: a bucket whose stems left the shelf by another route (a reused
	# bucket re-harvested or re-graded clears its old Shelf Items) still has its
	# un-issued rows outstanding, and total - allocated then goes negative.
	bas.available_quantity = max(0, flt(bas.total_quantity) - bas.allocated_quantity)
	return outstanding


# ============================================================
# ALLOCATE STOCK
# UPDATED: Set in_transit = 1 for remote farm allocations
# ============================================================
# Teams allowed to be stamped onto an OPL's custom_team during allocation.
ALLOWED_ALLOCATION_TEAMS = {"Team A", "Team B", "Jamafa", "Eldama", "Bravo"}


@frappe.whitelist()
def allocate_stock_with_buckets(
	sales_order: str | None,
	allocations: str | list | dict | None,
	location: str | None = None,
	teams: str | list | dict | None = None,
):
	if isinstance(allocations, str):
		allocations = json.loads(allocations)

	if isinstance(teams, str):
		teams = json.loads(teams)
	teams = teams or {}

	if not sales_order or not allocations:
		frappe.throw(_("Sales Order and allocations required"))

	if not location:
		frappe.throw(_("Location parameter is required"))

	# ── Team is mandatory PER LINE: every Sales Order Item being allocated must have a
	#    valid team. It is written to custom_team on that item's OPL (see impl). ──
	for so_item in {a["sales_order_item"] for a in allocations}:
		t = teams.get(so_item)
		if not t:
			frappe.throw(_("A team is required for every item being allocated."))
		if not frappe.db.exists("Packing Teams", t):
			frappe.throw(_("Invalid team: {0}").format(t))

	try:
		return _allocate_stock_with_buckets_impl(sales_order, allocations, location, teams)
	except Exception:
		frappe.db.rollback()
		raise


def _line_capacity(so_item):
	"""Stems a Sales Order line's boxes hold: Number of Boxes x stems per box -- the
	same rule the pick-list builders enforce (create_mixed_box_picklist "Overpacked")."""
	per_box = (
		so_item.get("custom_packrate_mixed_box")
		if so_item.get("custom_mixed_box") == 1
		else so_item.get("custom_packrate")
	)
	return int(per_box or 10) * int(so_item.get("custom_number_of_boxes") or 1)


def _fit_allocations_to_boxes(so_doc, allocations):
	"""Trim an allocation to the room left in each line's boxes, BEFORE anything is
	written, so it can never fail at the end as "Overpacked".

	Room = the line's capacity less the stems already on its pick rows. Buckets are
	taken in the order sent; the one that crosses the limit is drawn partially (its
	balance stays free on the shelf) and any after it are skipped. A line with no
	room left stops the allocation with a plain message. Returns (allocations, notes).
	"""
	lines = {i.name: i for i in so_doc.items}
	room = {}
	for soi in {a["sales_order_item"] for a in allocations}:
		item = lines.get(soi)
		if not item:
			continue
		placed = flt(
			frappe.db.sql(
				"""
				SELECT COALESCE(SUM(pli.stock_qty), 0)
				FROM `tabPick List Item` pli
				JOIN `tabOrder Pick List` opl ON opl.name = pli.parent
				WHERE pli.parenttype = 'Order Pick List' AND opl.docstatus < 2
				  AND %(soi)s IN (COALESCE(pli.custom_sale_order_item, ''), COALESCE(pli.sales_order_item, ''))
				""",
				{"soi": soi},
			)[0][0]
		)
		room[soi] = max(0.0, _line_capacity(item) - placed)

	fitted, notes, full = [], [], set()
	for a in allocations:
		soi = a["sales_order_item"]
		if soi not in room:
			fitted.append(a)
			continue
		qty = flt(a.get("qty"))
		take = min(qty, room[soi])
		if take <= 0:
			full.add(soi)
			notes.append(
				_("{0}: skipped bucket {1} ({2} stems), the line's boxes are full.").format(
					lines[soi].item_code, a.get("bucket_id"), int(qty)
				)
			)
			continue
		if take < qty:
			notes.append(
				_("{0}: took {1} of {2} stems from bucket {3}, the rest stays on its shelf.").format(
					lines[soi].item_code, int(take), int(qty), a.get("bucket_id")
				)
			)
			a = {**a, "qty": take}
		room[soi] -= take
		fitted.append(a)

	empty = [s for s in full if not any(f["sales_order_item"] == s for f in fitted)]
	if empty:
		frappe.throw(
			"<br>".join(
				_(
					"{0} {1}: all {2} stems ({3} boxes) are already on the pick list; nothing more fits. Raise Number of Boxes on the Sales Order to allocate more."
				).format(
					lines[s].item_code,
					lines[s].get("custom_length") or "",
					_line_capacity(lines[s]),
					int(lines[s].get("custom_number_of_boxes") or 1),
				)
				for s in empty
			),
			title=_("Line already full"),
		)
	return fitted, notes


def _allocate_stock_with_buckets_impl(sales_order, allocations, location, teams=None, target_opl=None):
	"""`target_opl` (packing_quality's replacement): put the new rows on that
	Order Pick List -- the one being packed -- whatever box type the line is,
	instead of the per-type OPL creators (the mixed ones skip a submitted OPL)."""
	config = _get_production_config()
	farm_config = config["farm_config"]
	farms_by_location = config["farms_by_location"]

	location_farms = set(farms_by_location.get(location, []))
	if not location_farms:
		frappe.throw(_("No farms configured for location: {0}").format(location))

	# Determine the sales shelf farm for this location
	sales_shelf_farm = None
	for farm in location_farms:
		if farm_config.get(farm, {}).get("sales_shelf"):
			sales_shelf_farm = farm
			break

	frappe.log_error(
		title="Incoming Allocation Request",
		message=f"SO: {sales_order}, Location: {location}\nSales Shelf Farm: {sales_shelf_farm}\nPayload: {json.dumps(allocations, indent=2)}",
	)

	so_doc = frappe.get_doc("Sales Order", sales_order)
	# Never allocate more than a line's boxes hold: fit it here, before anything is
	# written, instead of failing as "Overpacked" once the pick list is built.
	allocations, fit_notes = _fit_allocations_to_boxes(so_doc, allocations)
	business_unit = stock_movement.business_unit_of(so_doc)

	# ── Validate against confirmed stems ──
	confirmed_by_item = _get_confirmed_stems_for_farms(sales_order, list(location_farms))[0]

	alloc_totals_by_item = {}
	for a in allocations:
		so_item = a["sales_order_item"]
		alloc_totals_by_item[so_item] = alloc_totals_by_item.get(so_item, 0) + float(a.get("qty", 0))

	for so_item, new_qty in alloc_totals_by_item.items():
		confirmed = confirmed_by_item.get(so_item, 0)
		if confirmed > 0:
			existing_allocated = (
				frappe.db.sql(
					"""
                SELECT COALESCE(SUM(ba.quantity_allocated), 0)
                FROM `tabBucket Allocations` ba
                INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
                WHERE ba.sales_order_item = %s AND ba.cancelled = 0
            """,
					so_item,
				)[0][0]
				or 0
			)

			total_after = existing_allocated + new_qty
			if total_after > confirmed + 0.001:
				frappe.throw(
					f"Cannot allocate {int(new_qty)} stems for item. "
					f"This location confirmed {int(confirmed)} stems, "
					f"already allocated {int(existing_allocated)}. "
					f"Maximum remaining: {int(confirmed - existing_allocated)}"
				)

	# ── PRE-CHECK: Cross-location conflict ──
	so_item_ids = list({a["sales_order_item"] for a in allocations})
	si_placeholders = ", ".join(["%s"] * len(so_item_ids))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	existing_farm_rows = frappe.db.sql(
		f"""
        SELECT DISTINCT ba.sales_order_item, bas.shelf_farm
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item IN ({si_placeholders})
          AND ba.cancelled = 0
    """,
		so_item_ids,
		as_dict=True,
	)

	for row in existing_farm_rows:
		if row["shelf_farm"] not in location_farms:
			frappe.throw(
				f"Cannot allocate: item {row['sales_order_item']} already has allocations "
				f"from farm '{row['shelf_farm']}' which is in a different location. "
				f"Please refresh and try again."
			)

	# ── Fetch all shelf items for allocated buckets in ONE query ──
	bucket_ids = list({a.get("bucket_id") for a in allocations if a.get("bucket_id")})
	bid_placeholders = ", ".join(["%s"] * len(bucket_ids))
	farm_placeholders = ", ".join(["%s"] * len(location_farms))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	shelf_rows = frappe.db.sql(
		f"""
        SELECT
            si.bucket_id,
            si.variety AS item_code,
            si.stem_qty,
            si.stem_length,
            si.warehouse,
            si.date_added,
            COALESCE(si.harvest_date, si.date_added) AS harvest_date,
            si.parent AS shelf_location,
            s.farm
        FROM `tabShelf Item` si
        INNER JOIN `tabShelf` s ON s.name = si.parent
        WHERE si.bucket_id IN ({bid_placeholders})
          AND s.farm IN ({farm_placeholders})
    """,
		bucket_ids + list(location_farms),
		as_dict=True,
	)

	# A bucket legitimately carries more than one Shelf Item row for the same
	# (bucket_id, item_code): Spray Roses are graded straight in the field, so
	# ONE bucket can collect several varieties AND several stem lengths before
	# it's ever received -- e.g. Fireworks at both 62cm and 52cm in the same
	# bucket, each length becoming its own Shelf Item row. Those two rows are
	# two DIFFERENT allocatable pools (feeding two different Sales Order lines)
	# and must never be merged into one total.
	#
	# The only rows that SHOULD merge are true duplicates: the same bucket +
	# variety + LENGTH split across more than one row (confirmed live: bucket
	# V7U1X9 had two rows, 60 + 40 stems, same variety/length/shelf -- the
	# allocation planning screen lists both and so shows the bucket as "100
	# available", but a dict keyed only on (bucket_id, item_code) used to just
	# keep whichever row SQL returned last (40), silently dropping the other's
	# stems, or -- worse -- would silently sum two genuinely different lengths
	# together if keyed that way). Keying on (bucket_id, item_code, stem_length)
	# sums same-length duplicates while keeping different lengths separate.
	shelf_map = {}
	for r in shelf_rows:
		map_key = (r["bucket_id"], r["item_code"], r["stem_length"] or "")
		if map_key not in shelf_map:
			shelf_map[map_key] = dict(r)
		else:
			shelf_map[map_key]["stem_qty"] = (shelf_map[map_key].get("stem_qty") or 0) + (
				r.get("stem_qty") or 0
			)
			frappe.log_error(
				title="Duplicate Shelf Item rows for one bucket",
				message=(
					f"bucket_id={r['bucket_id']} item_code={r['item_code']} "
					f"stem_length={r['stem_length']}: more than one Shelf Item row exists for "
					f"this bucket+variety+length on the same farm. Their stem_qty was summed "
					f"for allocation, but the underlying rows should be reviewed/merged -- "
					f"shelf_location={r.get('shelf_location')}."
				),
			)

	for a in allocations:
		key = (a.get("bucket_id"), a.get("item_code"), a.get("stem_length") or "")
		if key not in shelf_map:
			any_shelf = frappe.db.sql(
				"""
                SELECT si.bucket_id, si.variety, s.farm
                FROM `tabShelf Item` si
                INNER JOIN `tabShelf` s ON s.name = si.parent
                WHERE si.bucket_id = %s
                LIMIT 1
            """,
				a.get("bucket_id"),
				as_dict=True,
			)

			if any_shelf:
				actual = any_shelf[0]
				frappe.throw(
					f"Bucket '{a['bucket_id']}' is on farm '{actual['farm']}' which is not in "
					f"location '{location}'. Please refresh and try again."
				)
			else:
				frappe.throw(
					f"Bucket '{a['bucket_id']}' not found on any shelf. "
					f"It may have been moved. Please refresh and try again."
				)

	# ── Fetch all BAS records for these buckets in ONE query ──
	variety_list = list({a["item_code"] for a in allocations})
	var_placeholders = ", ".join(["%s"] * len(variety_list))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	bas_rows = frappe.db.sql(
		f"""
        SELECT name, bucket_id, item_code, stem_length, total_quantity,
               allocated_quantity, available_quantity, in_transit
        FROM `tabBucket Allocation Status`
        WHERE bucket_id IN ({bid_placeholders})
          AND item_code IN ({var_placeholders})
    """,
		bucket_ids + variety_list,
		as_dict=True,
	)

	# Keyed with stem_length too -- see the shelf_map comment above. A bucket
	# holding Fireworks at both 62cm and 52cm needs two independent Bucket
	# Allocation Status records (one per length); without the length in the
	# key, the second length's allocation would read/write the FIRST length's
	# BAS record, corrupting both lengths' available quantity.
	bas_map = {(r["bucket_id"], r["item_code"], r["stem_length"] or ""): r for r in bas_rows}

	# ── Group allocations by (bucket, item, length) ──
	alloc_by_bucket = {}
	for a in allocations:
		alloc_by_bucket.setdefault((a["bucket_id"], a["item_code"], a.get("stem_length") or ""), []).append(a)

	for (bucket_id, item_code, stem_length), group in alloc_by_bucket.items():
		shelf = shelf_map[(bucket_id, item_code, stem_length)]
		is_sales_shelf = farm_config.get(shelf["farm"], {}).get("sales_shelf", 0)

		# ── TRANSIT CHECK: Is this bucket on a remote farm? ──
		needs_transfer = sales_shelf_farm and shelf["farm"] != sales_shelf_farm

		bas_key = (bucket_id, item_code, stem_length)

		if bas_key in bas_map:
			bas = frappe.get_doc("Bucket Allocation Status", bas_map[bas_key]["name"], for_update=True)
		else:
			bas = frappe.new_doc("Bucket Allocation Status")
			bas.bucket_id = bucket_id
			bas.item_code = item_code
			bas.total_quantity = float(shelf["stem_qty"] or 0)
			bas.stem_length = shelf["stem_length"] or ""
			bas.warehouse = (
				stock_movement.holding_warehouse(shelf["warehouse"], shelf["farm"], business_unit) or ""
			)
			bas.harvest_date = shelf.get("harvest_date") or shelf["date_added"]
			bas.shelf_location = shelf["shelf_location"]
			bas.shelf_farm = shelf["farm"]
			bas.in_transit = 0
			bas.insert(ignore_permissions=True)
			bas.allocated_quantity = 0
			bas.available_quantity = bas.total_quantity

		# Re-sync against the live shelf before checking: the stored figure is only
		# as fresh as the last write, and the shelf may have changed since.
		recompute_bas_quantities(bas, shelf_qty=shelf["stem_qty"])
		current_available = float(bas.available_quantity or 0)
		requested = sum(float(a.get("qty", 0)) for a in group)

		if requested > current_available:
			frappe.throw(
				f"Over-allocation on bucket {bucket_id}: "
				f"{current_available} available, {requested} requested. "
				"Please refresh and try again."
			)

		existing_so_items = {row.sales_order_item for row in bas.bucket_allocations if not row.cancelled}
		for a in group:
			if a["sales_order_item"] in existing_so_items:
				continue
			bas.append(
				"bucket_allocations",
				{
					"sales_order": sales_order,
					"sales_order_item": a["sales_order_item"],
					"quantity_allocated": float(a["qty"]),
					"cancelled": 0,
				},
			)

		recompute_bas_quantities(bas, shelf_qty=shelf["stem_qty"])

		# ── MARK IN TRANSIT if needs transfer ──
		# Flag only. The balance is deliberately NOT zeroed: the bucket carries
		# its full contents to the sales shelf, so whatever this order did not
		# take is still real stock another line can claim while it travels.
		# Zeroing it here also made the over-allocation check above reject every
		# later allocation from the same bucket with "0 available".
		if needs_transfer:
			bas.in_transit = 1
			frappe.log_error(
				title="Bucket Marked In Transit",
				message=f"Bucket: {bucket_id}, Farm: {shelf['farm']}, "
				f"Destination: {sales_shelf_farm}, Allocated: {bas.allocated_quantity}",
			)

		bas.save(ignore_permissions=True)

	# ── Update SO item flags based on actual cumulative coverage ──
	affected_so_items = {a["sales_order_item"] for a in allocations}
	for so_item in affected_so_items:
		frappe.db.set_value(
			"Sales Order Item",
			so_item,
			{
				"custom_fully_allocated": 1 if _so_item_is_fully_allocated(so_item) else 0,
				"custom_stock_available": 1,
			},
			update_modified=False,
		)

	# Capture which lines already had an OPL BEFORE this allocation (custom_opl is set
	# during creation below). The team stamp uses this to tell a freshly-created OPL
	# from a pre-existing one for first-allocation-wins.
	prior_opl_by_soi = {
		a["sales_order_item"]: frappe.db.get_value("Sales Order Item", a["sales_order_item"], "custom_opl")
		for a in allocations
	}

	# ── Create/update pick lists ──
	for a in allocations:
		shelf = shelf_map.get((a["bucket_id"], a["item_code"], a.get("stem_length") or ""), {})
		a["_shelf_farm"] = shelf.get("farm", "")
		a["_is_sales_shelf"] = farm_config.get(shelf.get("farm", ""), {}).get("sales_shelf", 0)
		# The shelf row is the server-side truth for where the stems are and how
		# long they are; the client sends both, so overwrite rather than default.
		# A remote farm's bucket stays on its farm's store until shelved at the hub.
		a["warehouse"] = stock_movement.holding_warehouse(
			shelf.get("warehouse") or a.get("warehouse"), a["_shelf_farm"], business_unit
		)
		a["stem_length"] = shelf.get("stem_length") or a.get("stem_length")

	if target_opl:
		pick_results = _add_rows_to_opl(target_opl, allocations, so_doc, location, confirmed_by_item)
	else:
		pick_results = _create_pick_list(sales_order, allocations, so_doc, location, confirmed_by_item)

	# ── Move the stems. Allocation is a sale, so the ledger has to follow the
	#    SO Warehouse Mapping all the way into a *Sold warehouse. Anything that
	#    cannot be backed by stock throws, and the caller rolls the whole
	#    allocation back — a pick list we cannot supply is worse than none. ──
	opl_by_soi = {
		a["sales_order_item"]: frappe.db.get_value("Sales Order Item", a["sales_order_item"], "custom_opl")
		for a in allocations
	}
	for a in allocations:
		a["_opl"] = target_opl or opl_by_soi.get(a["sales_order_item"])

	stock_moves = stock_movement.move_allocation_to_sold(
		allocations,
		business_unit=stock_movement.business_unit_of(so_doc),
		sales_order=sales_order,
	)

	# ── Stamp the per-line team onto each OPL this allocation created/updated.
	#    The team is taken from the Sales Order Item the OPL covers (straight boxes are
	#    one OPL per item, so each line keeps its own team). First allocation wins:
	#    only set custom_team when not already set, so a later allocation to an existing
	#    OPL never overwrites the team chosen on the first allocation. ──
	teams = teams or {}
	# The client's dropdown now sources this list from the Packing Teams
	# doctype (it used to be a hardcoded ["Team A","Team B","Jamafa","Eldama",
	# "Bravo"] that included a team, "Bravo", which was never a real Packing
	# Teams record) -- validate here too, so a stale cached browser tab still
	# running the old JS can never stamp a fake team onto a real OPL again.
	real_teams = set(frappe.get_all("Packing Teams", pluck="name"))
	for r in pick_results:
		opl_name = r.get("name")
		if not opl_name:
			continue
		opl_soi = frappe.db.get_value("Pick List Item", {"parent": opl_name}, "sales_order_item")
		opl_team = teams.get(opl_soi)
		if not opl_team or opl_team not in real_teams:
			continue
		# First-allocation-wins: if this line already had THIS OPL before the current
		# allocation, keep its team. Otherwise it is a freshly-created OPL, so write the
		# chosen team (overwriting the Select field's auto first-option value "Team A").
		if opl_name == prior_opl_by_soi.get(opl_soi):
			continue
		frappe.db.set_value("Order Pick List", opl_name, "team", opl_team, update_modified=False)

	return {
		"success": True,
		"message": "Allocation completed successfully" + "".join("<br>" + n for n in fit_notes),
		"fit_notes": fit_notes,
		"pick_list_results": pick_results,
		"stock_moves": stock_moves,
	}


# ============================================================
# PICK LIST CREATION
# ============================================================
def _create_pick_list(sales_order, allocations, so_doc, location, confirmed_by_item=None):
	straight = []
	mixed = []
	mixed_bunch = []

	for a in allocations:
		item = next((i for i in so_doc.items if i.name == a["sales_order_item"]), None)
		if not item:
			continue
		if item.get("custom_mixed_bunch"):
			mixed_bunch.append(a)
		elif item.custom_mixed_box:
			mixed.append(a)
		else:
			straight.append(a)

	results = []

	if straight:
		# Rule: one Sales Order Item = exactly one OPL for straight boxes.
		# Route per-SOI based on the existing OPL's state:
		#   - no OPL (or cancelled) → create a new one
		#   - draft OPL              → ORM append via _update_existing_pick_list
		#   - submitted OPL          → raw-SQL append via _append_rows_to_existing_opls
		by_soi = {}
		for a in straight:
			by_soi.setdefault(a["sales_order_item"], []).append(a)

		for soi, allocs in by_soi.items():
			existing_opl = frappe.db.get_value("Sales Order Item", soi, "custom_opl")
			parent_docstatus = None
			if existing_opl and frappe.db.exists("Order Pick List", existing_opl):
				parent_docstatus = frappe.db.get_value("Order Pick List", existing_opl, "docstatus")

			if parent_docstatus == 0:
				name = _update_existing_pick_list(
					existing_opl,
					allocs,
					so_doc,
					location=location,
					confirmed_by_item=confirmed_by_item,
				)
				status = (
					"submitted" if frappe.db.get_value("Order Pick List", name, "docstatus") == 1 else "draft"
				)
				results.append({"type": "straight", "status": status, "name": name})

			elif parent_docstatus == 1:
				for a in allocs:
					a["_existing_opl"] = existing_opl
				updated = _append_rows_to_existing_opls(allocs, so_doc, location)
				for opl_name in updated:
					results.append({"type": "straight", "status": "updated_existing", "name": opl_name})

			else:
				from upande_packhouse.server_scripts.create_straight_box_pick_list import (
					create_straight_box_pick_list_for_allocated_items,
				)

				names = (
					create_straight_box_pick_list_for_allocated_items(
						so_doc, allocs, submit=True, location=location
					)
					or []
				)
				for opl_name in names:
					status = (
						"submitted"
						if frappe.db.get_value("Order Pick List", opl_name, "docstatus") == 1
						else "draft"
					)
					results.append({"type": "straight", "status": status, "name": opl_name})

	if mixed:
		from upande_packhouse.server_scripts.create_mixed_box_picklist import (
			create_mixed_box_pick_list_for_allocated_items,
		)

		name = create_mixed_box_pick_list_for_allocated_items(
			sales_order_doc=so_doc, allocations=mixed, submit=True, location=location
		)
		status = (
			"submitted"
			if name and frappe.db.get_value("Order Pick List", name, "docstatus") == 1
			else "draft"
		)
		results.append({"type": "mixed", "status": status, "name": name})

	if mixed_bunch:
		from upande_packhouse.server_scripts.create_mixed_bunch_picklist import (
			create_mixed_bunch_pick_list_for_allocated_items,
		)

		name = create_mixed_bunch_pick_list_for_allocated_items(
			sales_order_doc=so_doc, allocations=mixed_bunch, submit=True, location=location
		)
		status = (
			"submitted"
			if name and frappe.db.get_value("Order Pick List", name, "docstatus") == 1
			else "draft"
		)
		results.append({"type": "mixed_bunch", "status": status, "name": name})

	return results


def _add_rows_to_opl(opl_name, allocations, so_doc, location, confirmed_by_item=None):
	"""Add `allocations` as rows of `opl_name`: the ORM append for a draft, the
	raw append for a submitted one (as _create_pick_list routes straight lines)."""
	docstatus = frappe.db.get_value("Order Pick List", opl_name, "docstatus")
	if docstatus == 0:
		name = _update_existing_pick_list(
			opl_name, allocations, so_doc, location=location, confirmed_by_item=confirmed_by_item
		)
		return [{"type": "target", "status": "draft", "name": name}]
	if docstatus == 1:
		for a in allocations:
			a["_existing_opl"] = opl_name
		return [
			{"type": "target", "status": "updated_existing", "name": n}
			for n in _append_rows_to_existing_opls(allocations, so_doc, location)
		]
	frappe.throw(_("Order Pick List {0} is cancelled.").format(opl_name))


def _append_rows_to_existing_opls(allocations, so_doc, location):
	"""Append rows to existing submitted OPLs via direct SQL."""
	by_opl = {}
	for a in allocations:
		by_opl.setdefault(a["_existing_opl"], []).append(a)

	updated_opls = []

	for opl_name, allocs in by_opl.items():
		max_idx = (
			frappe.db.sql(
				"SELECT COALESCE(MAX(idx), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
			)[0][0]
			or 0
		)

		max_box_id = (
			frappe.db.sql(
				"SELECT COALESCE(MAX(custom_box_id), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
			)[0][0]
			or 0
		)

		alloc_bucket_ids = [a.get("bucket_id") for a in allocs if a.get("bucket_id")]
		shelf_lookup = _fetch_shelf_for_buckets(alloc_bucket_ids, [a["item_code"] for a in allocs])

		for alloc in allocs:
			so_item_name = alloc["sales_order_item"]
			so_item = next((i for i in so_doc.items if i.name == so_item_name), None)
			if not so_item:
				continue

			existing = frappe.db.exists(
				"Pick List Item",
				{"parent": opl_name, "sales_order_item": so_item_name, "bucket": alloc.get("bucket_id")},
			)
			if existing:
				continue

			conv = so_item.conversion_factor or 1
			qty_uom = alloc["qty"] / conv if conv > 0 else alloc["qty"]
			max_idx += 1
			max_box_id += 1

			shelf_str = shelf_lookup.get((alloc.get("bucket_id"), alloc["item_code"]), "")
			is_sales_shelf = alloc.get("_is_sales_shelf", 1)
			awaiting_transfer = 0 if is_sales_shelf else 1

			row_name = frappe.generate_hash(length=10)

			frappe.db.sql(
				"""
                INSERT INTO `tabPick List Item` (
                    name, parent, parenttype, parentfield, idx, docstatus,
                    item_code, item_name, shelf, bucket,
                    custom_sale_order_item, farm,
                    source_warehouse, origin_warehouse, stem_length, transit_truck,
                    qty, stock_qty, picked_qty, stock_reserved_qty,
                    packrate, uom, conversion_factor,
                    stock_uom, delivered_qty,
                    custom_box_id,
                    sales_order_item,
                    custom_ready_for_packing, issued,
                    downgrade_reason,
                    available_stems_of_exact_length,
                    awaiting_transfer
                ) VALUES (
                    %(name)s, %(parent)s, 'Order Pick List', 'table_ytkc', %(idx)s, 1,
                    %(item_code)s, %(item_name)s, %(shelf)s, %(bucket)s,
                    %(so_item)s, %(farm)s,
                    %(warehouse)s, %(warehouse)s, %(stem_length)s, %(truck)s,
                    %(qty)s, %(stock_qty)s, 0, 0,
                    %(packrate)s, %(uom)s, %(conv)s,
                    %(stock_uom)s, 0,
                    %(box_id)s,
                    %(so_item)s,
                    1, 0,
                    %(downgrade_reason)s,
                    %(available_exact_stems)s,
                    %(awaiting_transfer)s
                )
            """,
				{
					"name": row_name,
					"parent": opl_name,
					"farm": alloc.get("_shelf_farm") or "",
					"idx": max_idx,
					"item_code": alloc["item_code"],
					"item_name": so_item.item_name,
					"shelf": shelf_str,
					"bucket": alloc.get("bucket_id"),
					"so_item": so_item_name,
					"item_group": so_item.item_group or "",
					"warehouse": _origin_warehouse(alloc.get("_shelf_farm"), alloc.get("warehouse")),
					"stem_length": alloc.get("stem_length") or so_item.custom_length or "",
					"truck": so_item.get("custom_truck") or "",
					"qty": qty_uom,
					"stock_qty": alloc["qty"],
					"packrate": so_item.get("custom_packrate") or "",
					"uom": so_item.uom,
					"conv": conv,
					"stock_uom": so_item.stock_uom,
					"box_id": max_box_id,
					"sales_order": so_doc.name,
					"downgrade_reason": alloc.get("downgrade_reason") or "",
					"available_exact_stems": alloc.get("available_exact_stems") or 0,
					"awaiting_transfer": awaiting_transfer,
				},
			)

		new_total = (
			frappe.db.sql(
				"SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %s", opl_name
			)[0][0]
			or 0
		)

		# Only update the running total. We no longer forcibly reset docstatus to 0
		# when an awaiting-transfer row is appended — un-submitting a previously
		# submitted OPL loses the audit trail and breaks downstream flows. If the
		# OPL was already submitted, we leave it submitted; the central helper
		# handles future state transitions.
		frappe.db.sql(
			"UPDATE `tabOrder Pick List` SET custom_total_stems = %s, modified = NOW() WHERE name = %s",
			[new_total, opl_name],
		)

		updated_opls.append(opl_name)

	# Ask the central helper to submit any drafts that are now fully covered.
	# Idempotent for already-submitted OPLs.
	for opl_name in updated_opls:
		try:
			_try_submit_opl_if_complete(opl_name)
		except Exception as e:
			frappe.log_error(title="OPL auto-submit (append path)", message=f"OPL: {opl_name}\n{e}")

	return updated_opls


def _fetch_shelf_for_buckets(bucket_ids, item_codes):
	"""Returns dict: (bucket_id, item_code) -> shelf_name string"""
	if not bucket_ids:
		return {}

	unique_buckets = list(set(bucket_ids))
	unique_items = list(set(item_codes))
	b_placeholders = ", ".join(["%s"] * len(unique_buckets))
	i_placeholders = ", ".join(["%s"] * len(unique_items))

	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	rows = frappe.db.sql(
		f"""
        SELECT si.bucket_id, si.variety AS item_code, si.parent AS shelf
        FROM `tabShelf Item` si
        WHERE si.bucket_id IN ({b_placeholders})
          AND si.variety IN ({i_placeholders})
    """,
		unique_buckets + unique_items,
		as_dict=True,
	)

	return {(r["bucket_id"], r["item_code"]): r["shelf"] for r in rows}


# ============================================================
# CENTRALIZED OPL SUBMISSION HELPER
# Single source of truth for "is this OPL ready to submit?"
# Called from every code path that creates or modifies an OPL.
# ============================================================
def _required_stems_for_so_item(so_item_name, confirmed_qty=None):
	"""Required stems = confirmed_qty if > 0, else custom_ordered_quantity,
	else qty × conversion_factor."""
	if confirmed_qty is not None and confirmed_qty > 0:
		return float(confirmed_qty)
	so_item = frappe.get_doc("Sales Order Item", so_item_name)
	if so_item.custom_ordered_quantity:
		return float(so_item.custom_ordered_quantity)
	return float((so_item.qty or 0) * (so_item.conversion_factor or 1))


def _cumulative_allocated_stems(so_item_name):
	"""SUM of all non-cancelled Bucket Allocations across the system for this SO item."""
	return (
		frappe.db.sql(
			"""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0)
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s
          AND (ba.cancelled = 0 OR ba.cancelled IS NULL)
    """,
			so_item_name,
		)[0][0]
		or 0
	)


def _so_item_is_fully_allocated(so_item_name, confirmed_qty=None):
	"""Cumulative coverage check. Used by the submission helper."""
	allocated = float(_cumulative_allocated_stems(so_item_name))
	required = _required_stems_for_so_item(so_item_name, confirmed_qty)
	return allocated >= required - 0.001


def opl_submit_blockers(opl):
	"""The ONE readiness check for "may this Order Pick List submit?" — every
	programmatic submit path (allocation-time, shelving-time, and the
	doctype's own before_submit backstop) calls this and this alone, so there
	is exactly one place that can ever be wrong instead of several
	independently-drifting copies.

	Takes an already-loaded Order Pick List DOCUMENT (not a name — this runs
	inside before_submit too, where the doc is in-memory and not yet
	re-fetchable in its final state). Returns a list of human-readable
	blocker strings; empty list = ready to submit.

	Deliberately ALWAYS uses the GLOBAL (all-farm) confirmed-stems total via
	_get_all_confirmed_stems, never a location-scoped one. Confirming stems
	is explicitly designed to be split across several farms/locations
	(confimSalesOrderItem lets each farm confirm part of one order line), so
	a location-scoped confirmed value can be smaller than the true total the
	order actually needs — passing that in as "required" let an OPL look
	fully allocated (allocated == that farm's own confirmed slice) while the
	order's real total was still short. Every caller used to compute and
	pass its own (location-scoped) confirmed_by_item into the submit
	decision; this function no longer accepts one, precisely to remove that
	whole class of mistake at the source.

	For mixed-box/mixed-bunch OPLs the "required" set is every SOI in the
	mix/bunch group on the parent SO — not just the SOIs already on this
	OPL — so the OPL stays in draft until the WHOLE group is allocated.
	For straight-box OPLs the required set is the SOIs currently on the OPL.
	"""
	blockers = []

	if not opl.table_ytkc:
		blockers.append("has no pick-list rows yet")
		return blockers

	awaiting = [
		loc
		for loc in opl.table_ytkc
		if (loc.get("awaiting_transfer") or 0)
		or (loc.get("in_transit") or 0)
		or ((loc.get("loaded_in_trolley") or 0) and not loc.get("shelved"))
	]
	if awaiting:
		blockers.append(
			"{0} bucket(s) are still in transit / not yet shelved at the sales farm ({1})".format(
				len(awaiting), ", ".join(sorted({loc.get("bucket") for loc in awaiting if loc.get("bucket")}))
			)
		)

	global_confirmed = _get_all_confirmed_stems(opl.sales_order)

	# NOTE: Order Pick List's own fields are "mix_group"/"bunch_group" (no
	# "custom_" prefix) -- that prefix only exists on the Sales Order Item
	# side (custom_mix_group/custom_bunch_group). Reading the wrong
	# (custom_-prefixed) name here always returned None, silently defeating
	# the whole-mix/bunch-group completeness check below for every mixed-box
	# and mixed-bunch OPL ever created -- confirmed against a real OPL
	# (mix_group="1" stored correctly by create_mixed_box_picklist.py, but
	# unreadable through this same wrong name) while testing this fix.
	opl_mix_group = opl.get("mix_group")
	opl_bunch_group = opl.get("bunch_group")
	if opl_mix_group:
		required_so_items = set(
			frappe.get_all(
				"Sales Order Item",
				filters={
					"parent": opl.sales_order,
					"custom_mix_group": opl_mix_group,
					"custom_mixed_box": 1,
				},
				pluck="name",
			)
		)
	elif opl_bunch_group:
		# Mixed-bunch OPL: the whole bunch group (every colour-line) must be
		# cumulatively allocated before the bouquet OPL may submit.
		required_so_items = set(
			frappe.get_all(
				"Sales Order Item",
				filters={
					"parent": opl.sales_order,
					"custom_bunch_group": opl_bunch_group,
					"custom_mixed_bunch": 1,
				},
				pluck="name",
			)
		)
	else:
		required_so_items = {loc.sales_order_item for loc in opl.table_ytkc if loc.sales_order_item}

	short = []
	for so_item in required_so_items:
		confirmed = global_confirmed.get(so_item)
		allocated = _cumulative_allocated_stems(so_item)
		required = _required_stems_for_so_item(so_item, confirmed_qty=confirmed)
		if allocated < required - 0.001:
			short.append("{0} ({1:g}/{2:g} stems)".format(so_item, allocated, required))
	if short:
		blockers.append("{0} line(s) not yet fully allocated: {1}".format(len(short), "; ".join(short)))

	return blockers


def _try_submit_opl_if_complete(opl_name, confirmed_by_item=None):
	"""Promote a draft OPL to submitted iff opl_submit_blockers finds nothing
	blocking. Idempotent — safe to call repeatedly.

	`confirmed_by_item` is accepted for backward compatibility with existing
	call sites but is IGNORED — see opl_submit_blockers's docstring for why
	the submit decision always recomputes confirmed stems globally instead
	of trusting a caller-supplied (possibly location-scoped) map.

	Returns: True if submitted (or already submitted), False if left as draft.
	"""
	if not opl_name:
		return False

	opl = frappe.get_doc("Order Pick List", opl_name)

	if opl.docstatus == 1:
		return True
	if opl.docstatus == 2:
		return False

	if opl_submit_blockers(opl):
		return False

	opl.flags.ignore_permissions = True
	opl.submit()
	return True


def _check_fully_allocated(so_item_name, added_qty, confirmed_qty=None):
	"""Check if a SO item is fully allocated."""
	if confirmed_qty is not None:
		required = confirmed_qty
	else:
		so_item = frappe.get_doc("Sales Order Item", so_item_name)
		required = so_item.custom_ordered_quantity or (so_item.qty * (so_item.conversion_factor or 1))

	allocated = (
		frappe.db.sql(
			"""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0)
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s
          AND (ba.cancelled = 0 OR ba.cancelled IS NULL)
    """,
			so_item_name,
		)[0][0]
		or 0
	)

	return (allocated + added_qty) >= required - 0.001


def _update_existing_pick_list(
	opl_name, new_allocations, so_doc, submit_if_complete=None, location=None, confirmed_by_item=None
):
	# submit_if_complete is retained for backward-compat but ignored.
	# The central helper now decides submission based on cumulative coverage.
	opl = frappe.get_doc("Order Pick List", opl_name)
	if opl.docstatus != 0:
		frappe.throw(f"Cannot update submitted Pick List {opl_name}")

	alloc_bucket_ids = [a.get("bucket_id") for a in new_allocations if a.get("bucket_id")]
	alloc_item_codes = [a["item_code"] for a in new_allocations]
	shelf_lookup = _fetch_shelf_for_buckets(alloc_bucket_ids, alloc_item_codes)

	box_id_counter = max([loc.custom_box_id or 0 for loc in opl.table_ytkc], default=0) + 1
	affected_items = set()
	has_awaiting_transfer = any(not a.get("_is_sales_shelf") for a in new_allocations)

	for alloc in new_allocations:
		so_item_name = alloc["sales_order_item"]
		affected_items.add(so_item_name)

		so_item = next((i for i in so_doc.items if i.name == so_item_name), None)
		if not so_item:
			continue

		conv = so_item.conversion_factor or 1
		qty_uom = alloc["qty"] / conv if conv > 0 else alloc["qty"]

		if any(
			loc.bucket == alloc.get("bucket_id") and loc.sales_order_item == so_item_name
			for loc in opl.table_ytkc
		):
			continue

		shelf_str = shelf_lookup.get((alloc.get("bucket_id"), alloc["item_code"]), "")
		is_sales_shelf = alloc.get("_is_sales_shelf", 1)

		opl.append(
			"table_ytkc",
			{
				"item_code": alloc["item_code"],
				"bucket": alloc.get("bucket_id"),
				"custom_sale_order_item": so_item_name,
				"item_name": so_item.item_name,
				"stock_uom": so_item.stock_uom,
				"uom": so_item.uom,
				"qty": qty_uom,
				"stock_qty": alloc["qty"],
				"conversion_factor": conv,
				"source_warehouse": _origin_warehouse(alloc.get("_shelf_farm"), alloc.get("warehouse")),
				"origin_warehouse": _origin_warehouse(alloc.get("_shelf_farm"), alloc.get("warehouse")),
				"sales_order_item": so_item.name,
				"stem_length": alloc.get("stem_length") or so_item.custom_length,
				"transit_truck": so_item.get("custom_truck"),
				"custom_box_id": box_id_counter,
				"shelf": shelf_str,
				"farm": alloc.get("_shelf_farm") or "",
				"downgrade_reason": alloc.get("downgrade_reason") or "",
				"available_stems_of_exact_length": alloc.get("available_exact_stems") or 0,
				"awaiting_transfer": 0 if is_sales_shelf else 1,
				"custom_ready_for_packing": 1,
			},
		)
		box_id_counter += 1

	total_stems = sum(loc.stock_qty for loc in opl.table_ytkc)
	opl.custom_total_stems = total_stems
	opl.save(ignore_permissions=True)

	# Central helper checks cumulative coverage across all OPLs/sessions and
	# promotes to submitted if every represented SO item is fully allocated.
	try:
		_try_submit_opl_if_complete(opl.name, confirmed_by_item=confirmed_by_item)
	except Exception as e:
		frappe.log_error(title="OPL auto-submit (update path)", message=f"OPL: {opl.name}\n{e}")

	return opl.name


# ============================================================
# UNALLOCATE
# UPDATED: Clear in_transit when unallocating
# ============================================================
@frappe.whitelist()
def unallocate_bucket_from_opl(sales_order_item: str, bucket_id: str, stem_length: str | None = None):
	frappe.db.begin()

	try:
		so_item = frappe.db.get_value(
			"Sales Order Item",
			sales_order_item,
			["parent", "item_code", "qty", "conversion_factor", "custom_length"],
			as_dict=True,
		)
		if not so_item:
			frappe.throw(_("Invalid sales order item"))

		sales_order = so_item.parent
		item_code = so_item.item_code

		# ── An issued line cannot be unallocated ────────────────────────────
		# Issuing physically takes the stems out of the bucket: the shelf row is
		# decremented and the stems are already on their way to the packhouse.
		# Cancelling the allocation now would reverse the ledger and drop the row
		# while nothing puts those stems back on the shelf, leaving them real in
		# the ERP but invisible to this page forever. Undo the issue first.
		issued_row = frappe.db.sql(
			"""
			SELECT ba.name
			FROM `tabBucket Allocations` ba
			INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
			WHERE bas.bucket_id = %(bucket)s
			  AND ba.sales_order_item = %(soi)s
			  AND ba.cancelled = 0
			  AND ba.issued = 1
			LIMIT 1
			""",
			{"bucket": bucket_id, "soi": sales_order_item},
		)
		if issued_row:
			frappe.throw(
				_(
					"Bucket {0} has already been issued for this line — its stems have left "
					"the shelf, so the allocation can no longer be cancelled here."
				).format(bucket_id)
			)

		# ── Put the stems back on the shelf ledger-wise: cancel the transfers
		#    that moved them into the Sold warehouse for this line. ──
		reversed_entries = stock_movement.reverse_allocation_movement(
			sales_order_item, bucket_id=bucket_id, item_code=item_code
		)

		# A bucket can hold this SAME variety at more than one stem length (see
		# the shelf_map comment in _allocate_stock_with_buckets_impl), so there
		# can be more than one Bucket Allocation Status row for (bucket_id,
		# item_code) -- one per length. Without a length to disambiguate,
		# get_value would grab an arbitrary one of them, unallocating the
		# wrong length's BAS row (or silently touching nothing while leaving
		# the real one untouched).
		#
		# Prefer the caller-supplied stem_length (the UI now sends the exact
		# batch's length the Unallocate button was clicked on). Fall back to
		# whichever BAS actually references this SO item's allocation, then to
		# the SO item's own length, for older callers that don't pass one.
		bas_name = None
		if stem_length is not None:
			bas_name = frappe.db.get_value(
				"Bucket Allocation Status",
				{"bucket_id": bucket_id, "item_code": item_code, "stem_length": stem_length or ""},
				"name",
			)

		if not bas_name:
			bas_candidates = frappe.get_all(
				"Bucket Allocation Status",
				filters={"bucket_id": bucket_id, "item_code": item_code},
				pluck="name",
			)
			for candidate in bas_candidates:
				if frappe.db.exists(
					"Bucket Allocations",
					{"parent": candidate, "sales_order_item": sales_order_item, "cancelled": 0},
				):
					bas_name = candidate
					break
			if not bas_name and len(bas_candidates) == 1:
				bas_name = bas_candidates[0]
			elif not bas_name and bas_candidates:
				bas_name = frappe.db.get_value(
					"Bucket Allocation Status",
					{
						"bucket_id": bucket_id,
						"item_code": item_code,
						"stem_length": so_item.custom_length or "",
					},
					"name",
				)

		bas_updated = False
		if bas_name:
			bas = frappe.get_doc("Bucket Allocation Status", bas_name, for_update=True)
			cancelled_any = False
			for row in bas.bucket_allocations:
				if row.sales_order_item == sales_order_item and not row.cancelled:
					row.cancelled = 1
					row.db_update()
					cancelled_any = True

			if cancelled_any:
				non_cancelled = recompute_bas_quantities(bas)

				# ── CLEAR IN_TRANSIT if no more allocations ──
				if not non_cancelled:
					bas.in_transit = 0

				bas.flags.ignore_validate = True
				bas.flags.ignore_mandatory = True
				bas.flags.ignore_permissions = True
				bas.save(ignore_permissions=True)
				bas_updated = True

		opls = frappe.get_all("Order Pick List", filters={"sales_order": sales_order}, pluck="name")
		removed_from_opls = []
		opls_deleted = []

		for opl_name in opls:
			rows = frappe.db.sql(
				"""
                SELECT name FROM `tabPick List Item`
                WHERE parent = %s AND sales_order_item = %s AND bucket = %s
            """,
				[opl_name, sales_order_item, bucket_id],
				as_dict=True,
			)

			if not rows:
				continue

			for row in rows:
				frappe.db.sql("DELETE FROM `tabPick List Item` WHERE name = %s", row.name)

			removed_from_opls.append(opl_name)

		for opl_name in removed_from_opls:
			remaining_count = (
				frappe.db.sql("SELECT COUNT(*) FROM `tabPick List Item` WHERE parent = %s", opl_name)[0][0]
				or 0
			)

			if remaining_count == 0:
				_force_delete_opl(opl_name)
				opls_deleted.append(opl_name)
			else:
				_reindex_opl_rows(opl_name)
				new_total = (
					frappe.db.sql(
						"SELECT COALESCE(SUM(stock_qty), 0) FROM `tabPick List Item` WHERE parent = %s",
						opl_name,
					)[0][0]
					or 0
				)
				frappe.db.sql(
					"UPDATE `tabOrder Pick List` SET custom_total_stems = %s, modified = NOW() WHERE name = %s",
					[new_total, opl_name],
				)

		remaining_allocated = (
			frappe.db.sql(
				"""
            SELECT COALESCE(SUM(ba.quantity_allocated), 0)
            FROM `tabBucket Allocations` ba
            INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
            WHERE ba.sales_order_item = %s AND ba.cancelled = 0
        """,
				sales_order_item,
			)[0][0]
			or 0
		)

		fully_unallocated = remaining_allocated == 0

		if fully_unallocated:
			frappe.db.set_value(
				"Sales Order Item",
				sales_order_item,
				{"custom_fully_allocated": 0, "custom_stock_available": 0, "custom_opl": None},
				update_modified=False,
			)
		else:
			frappe.db.set_value(
				"Sales Order Item",
				sales_order_item,
				{"custom_fully_allocated": 1 if _so_item_is_fully_allocated(sales_order_item) else 0},
				update_modified=False,
			)

		any_allocated = (
			frappe.db.sql(
				"""
            SELECT COUNT(*) FROM `tabSales Order Item`
            WHERE parent = %s AND custom_fully_allocated = 1
        """,
				sales_order,
			)[0][0]
			or 0
		)

		if any_allocated == 0:
			# `Sales Order.custom_stock_allocated` is a Custom Field owned by
			# upande_harvest / upande_kaitet, so the column is simply absent on a
			# site that runs the packhouse app alone — writing it blind raised
			# (1054, "Unknown column 'custom_stock_allocated' in 'SET'") and threw
			# the whole unallocation away. It is a display flag: never let it
			# block the unlink.
			try:
				if frappe.db.has_column("Sales Order", "custom_stock_allocated"):
					frappe.db.set_value(
						"Sales Order", sales_order, "custom_stock_allocated", 0, update_modified=False
					)
			except Exception:
				frappe.log_error("Could not clear Sales Order.custom_stock_allocated", frappe.get_traceback())

		frappe.db.commit()

		return {
			"success": True,
			"message": "Unallocated successfully",
			"removed_from_opls": removed_from_opls,
			"opls_deleted": opls_deleted,
			"bas_updated": bas_updated,
			"remaining_allocated": remaining_allocated,
			"so_flags_reset": fully_unallocated,
			"stock_entries_cancelled": reversed_entries,
		}

	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Unallocation Failed", frappe.get_traceback())
		return {"success": False, "message": f"Unallocation failed and was rolled back: {e!s}"}


# ============================================================
# REPLACE A REQUESTED BUCKET
# A remote farm cannot find an allocated bucket in its cold room: swap it for
# another shelved bucket of the same variety + stem length from the same farm,
# carry the allocation (BAS), the OPL rows and the Sold-leg stock across.
# ============================================================
def _requested_bucket_rows(pick_list_item, allow_shelved=False, allow_left=False, farm=None):
	"""Every OPL row that holds the same physical bucket as `pick_list_item`
	(an OPL has one row per box, so one bucket can span several rows).

	`allow_shelved` is for issuing (api/offline_issue.py): a bucket that was
	transferred and shelved at the packhouse is replaced there, from the farm it
	is shelved at now. Remote transfers leave it off -- a shelved bucket has
	left their cold room.

	`allow_left` is offline issuing's too: a remote-transfer bucket that left its
	farm (on a trolley or truck) and never turned up can still be replaced. `farm`
	is where the replacement comes from -- the station issuing it -- in place of
	the farm the missing bucket was shelved at."""
	anchor = frappe.db.get_value(
		"Pick List Item",
		pick_list_item,
		["name", "parent", "parenttype", "bucket", "item_code", "stem_length", "shelf", "farm"],
		as_dict=True,
	)
	if not anchor or anchor.parenttype != "Order Pick List" or not anchor.bucket:
		frappe.throw(_("Pick List Item {0} is not a bucket on an Order Pick List.").format(pick_list_item))

	rows = frappe.get_all(
		"Pick List Item",
		filters={"parent": anchor.parent, "parenttype": "Order Pick List", "bucket": anchor.bucket},
		fields=[
			"name",
			"item_code",
			"stem_length",
			"stock_qty",
			"sales_order_item",
			"source_warehouse",
			"warehouse",
			"shelf",
			"farm",
			"loaded_in_trolley",
			"in_transit",
			"shelved",
			"issued",
			"awaiting_transfer",
		],
	)
	for r in rows:
		if (r.item_code, r.stem_length or "") != (anchor.item_code, anchor.stem_length or ""):
			frappe.throw(
				_(
					"Bucket {0} carries more than one variety/length on {1}; replace it from the allocation page."
				).format(anchor.bucket, anchor.parent)
			)
		if (
			(cint(r.loaded_in_trolley) and not allow_left)
			or (cint(r.in_transit) and not allow_left)
			or (cint(r.shelved) and not allow_shelved)
			or cint(r.issued)
		):
			frappe.throw(
				_("Bucket {0} has already left the cold room and cannot be replaced.").format(anchor.bucket)
			)

	# The missing bucket's shelf decides the farm: the replacement must sit in the same cold room.
	shelf = anchor.shelf
	if allow_shelved:
		# Where the bucket is shelved now, not where it was picked from.
		shelf = frappe.db.get_value("Shelf Item", {"bucket_id": anchor.bucket}, "parent") or shelf
	farm = farm or (frappe.db.get_value("Shelf", shelf, "farm") if shelf else None) or anchor.farm
	if not farm:
		frappe.throw(_("Cannot tell which farm bucket {0} was allocated from.").format(anchor.bucket))
	return anchor, rows, farm


def _length_cm(value):
	"""'52cm' / '52' -> 52.0; anything unparseable -> None."""
	digits = "".join(ch for ch in str(value or "") if ch.isdigit() or ch == ".")
	try:
		return float(digits) if digits else None
	except ValueError:
		return None


def _replacement_candidates(anchor, farm, needed, limit=1):
	"""Unallocated shelved buckets of the same variety at the same farm, at the
	allocation's stem length or longer, holding at least `needed` stems.

	Same length first, then the nearest longer length, oldest harvest first.
	A bucket with ANY outstanding allocation (any variety or length) is never
	offered: replacing must not take stems another order is counting on.
	"""
	config = _get_production_config()
	discard_age = flt(config.get("discard_age") or 5)
	max_age = flt(config.get("farm_config", {}).get(farm, {}).get("max_allocation_age") or discard_age)
	min_cm = _length_cm(anchor.stem_length)
	taken = set(
		frappe.get_all(
			"Pick List Item",
			filters={"parent": anchor.parent, "parenttype": "Order Pick List"},
			pluck="bucket",
		)
	)

	# `Shelf Item.harvest_date` is packhouse-only; older shelf schemas only have date_added.
	harvest_expr = (
		"COALESCE(si.harvest_date, si.date_added)"
		if frappe.db.has_column("Shelf Item", "harvest_date")
		else "si.date_added"
	)
	# nosemgrep: frappe-sql-format-injection -- the holes are fixed SQL fragments; every value is bound
	rows = frappe.db.sql(
		f"""
		SELECT si.name AS shelf_item, si.parent AS shelf, si.bucket_id, si.variety,
		       si.stem_length, si.stem_qty, si.warehouse,
		       {harvest_expr} AS harvest_date,
		       (SELECT COALESCE(SUM(b.allocated_quantity), 0) FROM `tabBucket Allocation Status` b
		         WHERE b.bucket_id = si.bucket_id) AS allocated_qty,
		       (SELECT COALESCE(MAX(b.in_transit), 0) FROM `tabBucket Allocation Status` b
		         WHERE b.bucket_id = si.bucket_id) AS in_transit,
		       (SELECT COUNT(*) FROM `tabPick List Item` p
		         WHERE p.bucket = si.bucket_id AND p.parenttype = 'Order Pick List'
		           AND COALESCE(p.shelved, 0) = 0 AND COALESCE(p.issued, 0) = 0) AS open_picks
		FROM `tabShelf Item` si
		INNER JOIN `tabShelf` s ON s.name = si.parent
		WHERE si.variety = %(item)s
		  AND s.farm = %(farm)s
		  AND si.bucket_id != %(old)s
		  {DISCARD_EXCLUSION}
		ORDER BY harvest_date ASC, si.date_added ASC
		LIMIT 500
		""",
		{"item": anchor.item_code, "farm": farm, "old": anchor.bucket},
		as_dict=True,
	)

	today = frappe.utils.nowdate()
	out = []
	for r in rows:
		if r.bucket_id in taken or cint(r.in_transit) or cint(r.open_picks):
			continue
		if flt(r.allocated_qty) > stock_movement.QTY_TOLERANCE:
			continue
		cm = _length_cm(r.stem_length)
		if min_cm is not None and (cm is None or cm < min_cm):
			continue
		if min_cm is None and (r.stem_length or "") != (anchor.stem_length or ""):
			continue
		if flt(r.stem_qty) + stock_movement.QTY_TOLERANCE < needed:
			continue
		if r.harvest_date:
			age = frappe.utils.date_diff(today, str(r.harvest_date)[:10])
			if age >= discard_age or age > max_age:
				continue
		r["available_qty"] = flt(r.stem_qty)
		r["length_cm"] = cm
		out.append(r)
	# Same length first, then the nearest longer; the SQL order keeps FIFO within a length.
	out.sort(key=lambda r: (r.length_cm is None, (r.length_cm or 0) - (min_cm or 0)))
	return out[:limit]


def _no_replacement_message(anchor, farm, needed):
	return _("No unallocated {0} bucket of {1} or longer at {2} holds {3} stems.").format(
		anchor.item_code, anchor.stem_length or "", farm, int(needed)
	)


def _bucket_ledger(bucket_ids, item_code):
	"""Net stems of `item_code` per (bucket, warehouse), and per (bucket, SO item,
	warehouse) for the order-stamped legs, from the buckets' own Stock Entries.

	The entries that mention the buckets are joined as a derived table: written
	as `se.name IN (... UNION ...)` the lookup runs as a dependent subquery over
	the item's whole ledger (~20s a read on kaitet)."""
	buckets = tuple(bucket_ids)
	if stock_movement.line_has_bucket():
		bucket = "COALESCE(sed.custom_bucket_id, se.custom_bucket_id)"
		mentions = """SELECT parent AS name FROM `tabStock Entry Detail` WHERE custom_bucket_id IN %(buckets)s
			UNION SELECT name FROM `tabStock Entry` WHERE custom_bucket_id IN %(buckets)s"""
	else:
		bucket = "se.custom_bucket_id"
		mentions = "SELECT name FROM `tabStock Entry` WHERE custom_bucket_id IN %(buckets)s"
	# nosemgrep: frappe-sql-format-injection -- the holes are fixed SQL fragments; every value is bound
	lines = frappe.db.sql(
		f"""
		SELECT {bucket} AS bucket, se.custom_issued_to AS so_item,
		       sed.s_warehouse, sed.t_warehouse, sed.qty
		FROM ({mentions}) m
		JOIN `tabStock Entry` se ON se.name = m.name
		JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1 AND sed.item_code = %(item)s
		""",
		{"buckets": buckets, "item": item_code},
		as_dict=True,
	)
	held, sold = {}, {}
	for line in lines:
		if line.bucket not in buckets:
			continue
		for warehouse, sign in ((line.t_warehouse, 1), (line.s_warehouse, -1)):
			if not warehouse:
				continue
			held[(line.bucket, warehouse)] = held.get((line.bucket, warehouse), 0) + sign * flt(line.qty)
			if line.so_item:
				key = (line.bucket, line.so_item, warehouse)
				sold[key] = sold.get(key, 0) + sign * flt(line.qty)
	return held, sold


def _post_bucket_swap(
	*, source, target, item_code, lines, farm, business_unit, stem_length, so_item, opl, remarks
):
	"""One Sold-leg Stock Entry that trades buckets for an order: each line
	carries its own warehouses, so the replacement moves in and the missing
	bucket moves out in the same entry."""
	company = frappe.db.get_value("Warehouse", source, "company")
	cost_center = frappe.db.get_value(
		"Warehouse", source, "custom_cost_center"
	) or stock_movement.default_cost_center(company)
	if not cost_center:
		frappe.throw(f"Please contact your IT administrator to add the cost center for warehouse {source}")

	se = frappe.new_doc("Stock Entry")
	se.update(
		{
			"stock_entry_type": stock_movement.TYPE_TO_SOLD,
			"purpose": frappe.db.get_value("Stock Entry Type", stock_movement.TYPE_TO_SOLD, "purpose")
			or "Material Transfer",
			"company": company,
			"posting_date": frappe.utils.nowdate(),
			"posting_time": frappe.utils.nowtime(),
			"set_posting_time": 1,
			"from_warehouse": source,
			"to_warehouse": target,
			"farm": farm,
			"business_unit": business_unit,
			"custom_stem_length": stem_length,
			"custom_issued_to": so_item,
			"custom_opl_scanned": opl,
			"remarks": remarks,
			"cost_center": cost_center,
		}
	)
	for line in lines:
		se.append(
			"items",
			{
				"item_code": item_code,
				"qty": flt(line["qty"]),
				"s_warehouse": line["from"],
				"t_warehouse": line["to"],
				"farm": farm,
				"business_unit": business_unit,
				"custom_stem_length": line["stem_length"],
				"custom_bucket_id": line["bucket_id"],
				"cost_center": cost_center,
				"allow_zero_valuation_rate": 1,
			},
		)
	se.insert(ignore_permissions=True)
	se.submit()
	return se.name


def _clear_shelf_item(shelf_item, shelf, reason):
	"""Take one bucket row off its shelf, closing its Shelving Log like the issue flow does."""
	log = frappe.db.get_value("Shelving Log", {"shelf_item": shelf_item, "reason": "Shelved"}, "name")
	if log:
		frappe.db.set_value(
			"Shelving Log", log, {"reason": reason, "removed_on": now_datetime()}, update_modified=False
		)
	frappe.delete_doc("Shelf Item", shelf_item, force=1, ignore_permissions=True)
	frappe.db.set_value("Shelf", shelf, "modified", frappe.utils.now())


@frappe.whitelist()
def find_requested_bucket_replacement(pick_list_item: str, limit: int = 20):
	"""The buckets `replace_requested_bucket` could swap in, without changing
	anything: best match first (same length, then nearest longer; oldest
	harvest first). The top-level fields describe the best match, as before;
	`candidates` lists up to `limit` so the operator can pick a different one --
	replace_requested_bucket accepts any of them as `new_bucket_id`."""
	anchor, rows, farm = _requested_bucket_rows(pick_list_item)
	needed = sum(flt(r.stock_qty) for r in rows)
	found = _replacement_candidates(anchor, farm, needed, limit=max(1, min(int(limit or 20), 100)))
	if not found:
		return {
			"found": False,
			"message": _no_replacement_message(anchor, farm, needed),
		}

	def describe(c):
		return {
			"new_bucket": c.bucket_id,
			"shelf": c.shelf,
			"variety": c.variety,
			"stem_length": c.stem_length,
			"available_qty": c.available_qty,
			"harvest_date": str(c.harvest_date)[:10] if c.harvest_date else None,
		}

	return {
		"found": True,
		"old_bucket": anchor.bucket,
		"needed_qty": needed,
		**describe(found[0]),
		"candidates": [describe(c) for c in found],
	}


# The Bucket Requests app gives up on a request after 30s, while a blocked query
# waits innodb_lock_wait_timeout (50s) -- so a swap stuck behind another stock
# posting reached the operator as a bare "network error". Cap the wait per attempt
# and retry the whole swap (it rolls back cleanly) a few times inside that budget.
REPLACE_LOCK_WAIT_S = 6
REPLACE_ATTEMPTS = 3


@frappe.whitelist(methods=["POST"])
def replace_requested_bucket(
	pick_list_item: str,
	new_bucket_id: str | None = None,
	reason: str | None = None,
	notes: str | None = None,
	keep_old_on_shelf: bool = False,
):
	"""Swap a missing requested bucket for a matching one from the same farm,
	retrying when the swap loses a lock race (see REPLACE_LOCK_WAIT_S). `reason`
	(Missing / Damaged / Wrong variety / Other) and `notes` go on the Bucket
	Replacement record. `keep_old_on_shelf`: the old bucket is there, just
	mislabelled -- it stays on its shelf for its record to be corrected."""
	import time

	previous = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
	frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (int(REPLACE_LOCK_WAIT_S),))
	try:
		for attempt in range(REPLACE_ATTEMPTS):
			res = _replace_requested_bucket(
				pick_list_item, new_bucket_id, reason=reason, notes=notes, keep_old_on_shelf=keep_old_on_shelf
			)
			if not res.pop("_lock_conflict", False):
				return res
			if attempt + 1 < REPLACE_ATTEMPTS:
				time.sleep(0.5 * (attempt + 1))
		frappe.log_error("Replace Requested Bucket Failed", res.get("message") or "lock conflict")
		return {
			"success": False,
			"message": _(
				"Another stock posting is holding the records this swap needs. Try the replacement again."
			),
		}
	finally:
		frappe.db.sql("SET SESSION innodb_lock_wait_timeout = %s", (int(previous),))


def _replace_requested_bucket(
	pick_list_item: str,
	new_bucket_id: str | None = None,
	reason: str | None = None,
	notes: str | None = None,
	allow_shelved: bool = False,
	keep_old_on_shelf: bool = False,
	allow_left: bool = False,
	farm: str | None = None,
	to_remote: bool = False,
):
	"""Swap a missing requested bucket for a matching one from the same farm.

	`to_remote` (issuing, nothing matching at the sales farm): `farm` is a remote
	farm and the replacement is requested from there. The rows stay on their OPL,
	submitted or not, but go back to waiting for a truck: awaiting transfer, not
	ready for packing, the bucket left on its remote shelf for the farm to load.
	Arrival at the sales farm makes them ready again (mobile.api
	_shelve_update_transit_status); its sale is posted then (post_sale_on_arrival).

	The replacement is an unallocated bucket of the same variety at the
	allocation's stem length or longer (a longer one is recorded as a downgrade,
	the way the allocation page does it). Everything moves in one transaction:
	- Bucket Allocation Status: the old bucket's rows for these SO items are
	  cancelled, the new bucket (at its own length) gets the same quantities.
	- Order Pick List: each row of the old bucket now points at the new bucket,
	  its shelf, warehouse and stem length.
	- Shelf: the new bucket leaves its shelf; the missing bucket's row is
	  cleared too once nothing else is allocated from it.
	- Stock: one Sold-leg entry per SO item trades the buckets in place. The
	  replacement goes from wherever its stems sit on the route into the Sold
	  warehouse, and the missing bucket's stems for this order go from Sold back
	  to its shelf warehouse -- the positions its earlier legs reached are reused
	  instead of walking the route back and forward leg by leg.
	"""
	try:
		anchor, rows, farm = _requested_bucket_rows(
			pick_list_item, allow_shelved=allow_shelved, allow_left=allow_left, farm=farm
		)
		old_bucket = anchor.bucket
		opl_name = anchor.parent
		needed = sum(flt(r.stock_qty) for r in rows)

		candidates = _replacement_candidates(anchor, farm, needed, limit=500 if new_bucket_id else 1)
		if new_bucket_id:
			candidates = [c for c in candidates if c.bucket_id == new_bucket_id]
		if not candidates:
			frappe.throw(_no_replacement_message(anchor, farm, needed))
		new = candidates[0]

		qty_by_so_item = {}
		for r in rows:
			qty_by_so_item[r.sales_order_item] = qty_by_so_item.get(r.sales_order_item, 0) + flt(r.stock_qty)
		sales_order = frappe.db.get_value("Order Pick List", opl_name, "sales_order")
		so_doc = frappe.get_doc("Sales Order", sales_order)
		business_unit = stock_movement.business_unit_of(so_doc)
		# A replacement from a remote shelf waits in its farm's store, like any allocation.
		new.warehouse = stock_movement.holding_warehouse(new.warehouse, farm, business_unit)

		# ── Stock: where each bucket's stems sit now ──
		held, sold = _bucket_ledger([old_bucket, new.bucket_id], anchor.item_code)
		route = stock_movement.resolve_route(new.warehouse, business_unit) if new.warehouse else []
		sold_warehouse = next((hop["to"] for hop in route if hop["terminal"]), None)
		if not sold_warehouse:
			frappe.throw(_("No Sold warehouse is mapped for {0}.").format(new.warehouse or new.shelf))
		# Furthest position first: an earlier allocation may already have carried it past its shelf.
		positions = [new.warehouse] + [hop["to"] for hop in route if not hop["terminal"]]
		new_source = next(
			(
				wh
				for wh in reversed(positions)
				if held.get((new.bucket_id, wh), 0) + stock_movement.QTY_TOLERANCE >= needed
			),
			None,
		)
		if not new_source:
			frappe.throw(
				_("Bucket {0} has no {1} stems in stock at {2}.").format(
					new.bucket_id, int(needed), ", ".join(positions)
				)
			)
		old_home = rows[0].source_warehouse or rows[0].warehouse or new_source
		# Where the missing bucket's sale took its stems from: for a remote bucket that is
		# the arrival warehouse (Kapkolia Receiving), not its farm's cold store — sending
		# them back there would put Kapkolia's stems on the farm's books.
		old_return = next(
			(hop["from"] for hop in stock_movement.resolve_route(old_home, business_unit) if hop["terminal"]),
			old_home,
		)
		# A remote replacement still at its farm is sold when it is shelved at the sales
		# farm (post_sale_on_arrival), like any remote allocation — not now.
		defer_new = new_source == new.warehouse and stock_movement.needs_transfer(
			new.warehouse, business_unit
		)

		# ── BAS: release the old bucket ──
		old_bas_name = frappe.db.get_value(
			"Bucket Allocation Status",
			{"bucket_id": old_bucket, "item_code": anchor.item_code, "stem_length": anchor.stem_length or ""},
			"name",
		)
		was_in_transit = any(cint(r.awaiting_transfer) for r in rows)
		if old_bas_name:
			old_bas = frappe.get_doc("Bucket Allocation Status", old_bas_name, for_update=True)
			was_in_transit = was_in_transit or cint(old_bas.in_transit)
			for row in old_bas.bucket_allocations:
				if row.sales_order_item in qty_by_so_item and not row.cancelled:
					row.cancelled = 1
					row.db_update()
			if not recompute_bas_quantities(old_bas):
				old_bas.in_transit = 0
			old_bas.flags.ignore_validate = True
			old_bas.flags.ignore_mandatory = True
			old_bas.save(ignore_permissions=True)

		# ── Shelf: the missing bucket's row goes once nothing else is allocated from it ──
		# Unless it is not missing at all, only mislabelled (wrong variety): it stays
		# on its shelf for its record to be corrected.
		if keep_old_on_shelf:
			pass
		elif not old_bas_name or not flt(
			frappe.db.get_value("Bucket Allocation Status", old_bas_name, "allocated_quantity")
		):
			for si in frappe.get_all(
				"Shelf Item",
				filters={
					"bucket_id": old_bucket,
					"variety": anchor.item_code,
					"stem_length": anchor.stem_length or "",
				},
				fields=["name", "parent"],
			):
				_clear_shelf_item(si.name, si.parent, "Shelf Cleared")
			if old_bas_name:
				# Off its shelf: don't keep pointing the missing bucket at it.
				frappe.db.set_value("Bucket Allocation Status", old_bas_name, "shelf_location", "")

		# ── BAS: claim the new bucket (at its own stem length) ──
		new_bas_name = frappe.db.get_value(
			"Bucket Allocation Status",
			{"bucket_id": new.bucket_id, "item_code": anchor.item_code, "stem_length": new.stem_length or ""},
			"name",
		)
		if new_bas_name:
			new_bas = frappe.get_doc("Bucket Allocation Status", new_bas_name, for_update=True)
		else:
			new_bas = frappe.new_doc("Bucket Allocation Status")
			new_bas.bucket_id = new.bucket_id
			new_bas.item_code = anchor.item_code
			new_bas.stem_length = new.stem_length or ""
			new_bas.warehouse = new.warehouse or ""
			new_bas.harvest_date = new.harvest_date
			new_bas.shelf_location = new.shelf
			new_bas.shelf_farm = farm
			new_bas.total_quantity = flt(new.stem_qty)
			new_bas.in_transit = 0
			new_bas.insert(ignore_permissions=True)
		recompute_bas_quantities(new_bas, shelf_qty=new.stem_qty)
		if needed > flt(new_bas.available_quantity) + stock_movement.QTY_TOLERANCE:
			frappe.throw(
				_("Bucket {0} has {1} stems free, {2} needed.").format(
					new.bucket_id, flt(new_bas.available_quantity), needed
				)
			)
		for so_item, qty in qty_by_so_item.items():
			new_bas.append(
				"bucket_allocations",
				{
					"sales_order": sales_order,
					"sales_order_item": so_item,
					"quantity_allocated": qty,
					"cancelled": 0,
				},
			)
		recompute_bas_quantities(new_bas, shelf_qty=new.stem_qty)
		if was_in_transit or to_remote:
			new_bas.in_transit = 1
		new_bas.save(ignore_permissions=True)

		# ── OPL: point every row of the old bucket at the new one ──
		longer = (new.stem_length or "") != (anchor.stem_length or "")
		for r in rows:
			values = {
				"bucket": new.bucket_id,
				"shelf": new.shelf,
				# The replacement starts where it sits: its farm's own store.
				"source_warehouse": _origin_warehouse(farm, new.warehouse or r.source_warehouse),
				"origin_warehouse": _origin_warehouse(farm, new.warehouse or r.source_warehouse),
				# The pick row keeps the graded length; a longer one is a downgrade.
				"stem_length": new.stem_length or r.stem_length,
			}
			if r.warehouse:
				values["warehouse"] = new.warehouse or r.warehouse
			if allow_left and (cint(r.loaded_in_trolley) or cint(r.in_transit) or cint(r.awaiting_transfer)):
				# The missing bucket was on its way from a remote farm; the replacement
				# is here already, so the row is no longer travelling.
				values.update(
					{"loaded_in_trolley": 0, "in_transit": 0, "awaiting_transfer": 0, "transit_truck": ""}
				)
			if to_remote:
				# Requested from the remote farm: waiting for a truck again, wherever it was.
				values.update(
					{
						"farm": farm,
						"awaiting_transfer": 1,
						"loaded_in_trolley": 0,
						"in_transit": 0,
						"shelved": 0,
						"custom_ready_for_packing": 0,
						"transit_truck": "",
					}
				)
			if longer:
				values["downgrade_reason"] = _("Replacement for missing bucket {0} ({1})").format(
					old_bucket, anchor.stem_length or ""
				)
			frappe.db.set_value("Pick List Item", r.name, values)

		# ── Shelf: the replacement leaves its shelf now it is claimed for this order ──
		# A remote one stays on its farm's shelf until it is loaded there for the truck.
		if not to_remote:
			_clear_shelf_item(new.shelf_item, new.shelf, "Replaced")

		# ── Stock: trade the buckets in the Sold warehouse, one entry per SO item ──
		stock_moves = []
		for so_item, qty in qty_by_so_item.items():
			lines = []
			if not defer_new:
				lines.append(
					{
						"bucket_id": new.bucket_id,
						"qty": qty,
						"from": new_source,
						"to": sold_warehouse,
						"stem_length": new.stem_length,
					}
				)
			for (bucket, line_so_item, warehouse), outstanding in sold.items():
				if (
					bucket == old_bucket
					and line_so_item == so_item
					and warehouse != old_return
					and outstanding > stock_movement.QTY_TOLERANCE
				):
					lines.append(
						{
							"bucket_id": old_bucket,
							"qty": outstanding,
							"from": warehouse,
							"to": old_return,
							"stem_length": anchor.stem_length,
						}
					)
			if not lines:
				continue
			entry = _post_bucket_swap(
				source=new_source,
				target=sold_warehouse,
				item_code=anchor.item_code,
				lines=lines,
				farm=farm,
				business_unit=business_unit,
				stem_length=new.stem_length,
				so_item=so_item,
				opl=opl_name,
				remarks=f"Bucket {old_bucket} missing at {farm} — replaced with {new.bucket_id}",
			)
			stock_moves.append({"entry": entry, "so_item": so_item, "lines": lines})

		frappe.get_doc("Order Pick List", opl_name).add_comment(
			"Info",
			_("Bucket {0} ({1}) replaced with {2} ({3}, {4} stems).").format(
				old_bucket, anchor.stem_length or "", new.bucket_id, new.stem_length or "", int(needed)
			)
			+ (" " + _("Requested from {0}; waiting for transfer.").format(farm) if to_remote else ""),
		)
		# The record of what happened: which bucket, why, who, where it should have been.
		from upande_packhouse.api import bucket_replacement

		replacement = bucket_replacement.record(
			old_bucket=old_bucket,
			new_bucket=new.bucket_id,
			anchor=anchor,
			rows=rows,
			farm=farm,
			opl_name=opl_name,
			new_shelf=new.shelf,
			stems=needed,
			stock_entries=[m.get("entry") for m in stock_moves],
			reason=reason,
			notes=notes,
		)
		frappe.db.commit()
		return {
			"success": True,
			"message": _("Bucket {0} replaced with {1}.").format(old_bucket, new.bucket_id),
			"remote_farm": farm if to_remote else None,
			"opl": opl_name,
			"old_bucket": old_bucket,
			"new_bucket": new.bucket_id,
			"replacement": replacement,
			"shelf": new.shelf,
			"stem_length": new.stem_length,
			"warehouse": new.warehouse,
			"pick_list_items": [r.name for r in rows],
			"stock_moves": stock_moves,
		}
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as e:
		# A lost lock race, not a bad request: the caller retries the whole swap.
		frappe.db.rollback()
		return {"success": False, "message": str(e), "_lock_conflict": True}
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Replace Requested Bucket Failed", frappe.get_traceback())
		return {"success": False, "message": str(e)}


def _force_delete_opl(opl_name):
	try:
		doc = frappe.get_doc("Order Pick List", opl_name)
		if doc.docstatus == 1:
			doc.flags.ignore_permissions = True
			doc.flags.ignore_validate = True
			doc.flags.ignore_links = True
			doc.cancel()
			frappe.db.commit()
		frappe.delete_doc(
			"Order Pick List",
			opl_name,
			force=True,
			ignore_permissions=True,
			ignore_on_trash=True,
			delete_permanently=True,
		)
		frappe.db.commit()
	except Exception:
		frappe.log_error("OPL Deletion Failed", frappe.get_traceback())


def _reindex_opl_rows(opl_name):
	rows = frappe.db.sql(
		"SELECT name FROM `tabPick List Item` WHERE parent = %s ORDER BY idx", opl_name, as_dict=True
	)
	for i, row in enumerate(rows, 1):
		frappe.db.sql("UPDATE `tabPick List Item` SET idx = %s WHERE name = %s", [i, row.name])


# ============================================================
# HELPER: get farms for a location (used by frontend if needed)
# ============================================================
@frappe.whitelist()
def get_farms_for_location(location: str | None):
	config = _get_production_config()
	farm_config = config["farm_config"]
	farms = config["farms_by_location"].get(location, [])
	return [
		{
			"farm": f,
			"sales_shelf": farm_config.get(f, {}).get("sales_shelf", 0),
			"max_allocation_age": farm_config.get(f, {}).get("max_allocation_age", 5),
		}
		for f in farms
	]


# ============================================================
# SUBSTITUTE VARIETY
# ============================================================
@frappe.whitelist()
def get_substitute_varieties(
	sales_order: str | None,
	sales_order_item: str | None,
	item_code: str | None,
	location: str | None = None,
	color: str | None = None,
	headsize: str | int | float | None = None,
):
	"""
	Returns available varieties that can substitute the current item.
	When color/headsize are provided, filters to matching varieties (recommended).
	When not provided, returns all varieties in the same item group with available stock.

	Returns: list of { item_code, item_name, color, headsize, available_qty }
	"""
	if not sales_order or not sales_order_item:
		return []

	config = _get_production_config()
	farms_by_location = config["farms_by_location"]
	farm_config = config["farm_config"]
	discard_age = config["discard_age"]

	# Get the item group of the current item so we only suggest same-group varieties
	current_item = frappe.db.get_value(
		"Item", item_code, ["item_group", "custom_headsize_cm", "custom_color"], as_dict=True
	)

	if not current_item:
		return []

	item_group = current_item.get("item_group") or ""

	# Get active farms for this location
	location_farms = farms_by_location.get(location, []) if location else []
	if not location_farms:
		return []

	farm_placeholders = ", ".join(["%s"] * len(location_farms))

	# Build conditions for color/headsize filtering
	item_conditions = ["i.item_group = %s", "i.disabled = 0"]
	item_params = [item_group]

	if color:
		item_conditions.append("i.custom_color = %s")
		item_params.append(color)

	if headsize:
		item_conditions.append("i.custom_headsize_cm = %s")
		item_params.append(headsize)

	item_where = " AND ".join(item_conditions)

	# Find varieties that have stock on shelves at this location. In-transit
	# buckets count: they are allocatable (see get_sales_order_items_with_buckets),
	# so leaving them out here would understate a substitute's availability.
	# nosemgrep: frappe-sql-format-injection -- the only holes are `%s` placeholder lists sized from len(); every value is bound
	varieties = frappe.db.sql(
		f"""
        SELECT
            i.name AS item_code,
            i.item_name,
            i.custom_color AS color,
            i.custom_headsize_cm AS headsize,
            COALESCE(stock.available_qty, 0) AS available_qty
        FROM `tabItem` i
        LEFT JOIN (
            SELECT
                si.variety AS item_code,
                SUM(GREATEST(0, COALESCE(si.stem_qty, 0) - COALESCE(bas.allocated_quantity, 0))) AS available_qty
            FROM (
                -- One row per (bucket, variety, length, shelf). Shelving can write the
                -- same bucket's stems as several rows (one per receiving row), and a
                -- per-row LEFT JOIN to Bucket Allocation Status would subtract the
                -- whole allocation from EACH row -- which is how the page showed
                -- -40 / -30 for a 70-stem bucket fully allocated.
                SELECT bucket_id, variety, stem_length, parent,
                       SUM(COALESCE(stem_qty, 0)) AS stem_qty,
                       MIN(date_added) AS date_added,
                       MIN(harvest_date) AS harvest_date,
                       MIN(warehouse) AS warehouse,
                       MIN(cut_stage) AS cut_stage
                FROM `tabShelf Item`
                GROUP BY bucket_id, variety, stem_length, parent
            ) si
            INNER JOIN `tabShelf` s ON s.name = si.parent
            LEFT JOIN `tabBucket Allocation Status` bas
                ON bas.bucket_id = si.bucket_id AND bas.item_code = si.variety
                AND COALESCE(bas.stem_length, '') = COALESCE(si.stem_length, '')
            WHERE s.farm IN ({farm_placeholders})
              AND DATEDIFF(CURDATE(), COALESCE(si.harvest_date, si.date_added)) < %s
              {DISCARD_EXCLUSION}
            GROUP BY si.variety
        ) stock ON stock.item_code = i.name
        WHERE {item_where}
        HAVING available_qty > 0 OR i.name = %s
        ORDER BY
            CASE WHEN i.name = %s THEN 0 ELSE 1 END,
            CASE WHEN i.custom_color = %s THEN 0 ELSE 1 END,
            CASE WHEN i.custom_headsize_cm = %s THEN 0 ELSE 1 END,
            available_qty DESC
        LIMIT 50
    """,
		location_farms
		+ [discard_age]
		+ item_params
		+ [
			item_code,
			item_code,
			current_item.get("custom_color") or "",
			current_item.get("custom_headsize_cm") or "",
		],
		as_dict=True,
	)

	return varieties


@frappe.whitelist()
def substitute_variety(sales_order: str | None, sales_order_item: str | None, new_item_code: str | None):
	"""
	Substitutes the variety on a Sales Order Item.
	Updates item_code and item_name on the SO item row.
	Only allowed if the item has no existing allocations.

	Returns: { success: True/False, message: str }
	"""
	if not sales_order or not sales_order_item or not new_item_code:
		return {"success": False, "message": "Missing required parameters"}

	# Validate SO exists and is submitted
	so_doc = frappe.get_doc("Sales Order", sales_order)
	if so_doc.docstatus != 1:
		return {"success": False, "message": "Sales Order is not submitted"}

	# Find the SO item row
	so_item = None
	for item in so_doc.items:
		if item.name == sales_order_item:
			so_item = item
			break

	if not so_item:
		return {"success": False, "message": f"Sales Order Item {sales_order_item} not found"}

	old_item_code = so_item.item_code
	old_item_name = so_item.item_name

	if old_item_code == new_item_code:
		return {"success": False, "message": "New variety is the same as current variety"}

	# Check no existing allocations for this item
	existing_alloc = (
		frappe.db.sql(
			"""
        SELECT COALESCE(SUM(ba.quantity_allocated), 0) AS total
        FROM `tabBucket Allocations` ba
        INNER JOIN `tabBucket Allocation Status` bas ON bas.name = ba.parent
        WHERE ba.sales_order_item = %s AND ba.cancelled = 0
    """,
			sales_order_item,
		)[0][0]
		or 0
	)

	if existing_alloc > 0:
		return {
			"success": False,
			"message": f"Cannot substitute: {int(existing_alloc)} stems already allocated. "
			f"Unallocate first before substituting.",
		}

	# Check the item has an existing OPL — block if submitted
	existing_opl = frappe.db.get_value("Sales Order Item", sales_order_item, "custom_opl")
	if existing_opl:
		opl_status = frappe.db.get_value("Order Pick List", existing_opl, "docstatus")
		if opl_status == 1:
			return {
				"success": False,
				"message": f"Cannot substitute: item is on submitted pick list {existing_opl}. "
				f"Cancel or remove from pick list first.",
			}

	# Fetch new item details
	new_item = frappe.db.get_value(
		"Item", new_item_code, ["name", "item_name", "item_group", "stock_uom", "description"], as_dict=True
	)

	if not new_item:
		return {"success": False, "message": f"Item {new_item_code} not found"}

	# Update the Sales Order Item via direct SQL (since SO is submitted)
	try:
		frappe.db.sql(
			"""
            UPDATE `tabSales Order Item`
            SET item_code = %s,
                item_name = %s,
                description = %s,
                modified = NOW()
            WHERE name = %s
        """,
			[new_item_code, new_item.item_name, new_item.description or new_item.item_name, sales_order_item],
		)

		# Update SO modified timestamp
		frappe.db.sql(
			"""
            UPDATE `tabSales Order`
            SET modified = NOW()
            WHERE name = %s
        """,
			[sales_order],
		)

		frappe.db.commit()

		frappe.log_error(
			title="Variety Substitution",
			message=f"SO: {sales_order}, Item: {sales_order_item}\n"
			f"Changed: {old_item_code} ({old_item_name}) → {new_item_code} ({new_item.item_name})",
		)

		return {
			"success": True,
			"message": f"Substituted {old_item_name} with {new_item.item_name}",
			"old_item_code": old_item_code,
			"new_item_code": new_item_code,
			"new_item_name": new_item.item_name,
		}

	except Exception as e:
		frappe.db.rollback()
		frappe.log_error("Variety Substitution Failed", frappe.get_traceback())
		return {"success": False, "message": f"Substitution failed: {e!s}"}
