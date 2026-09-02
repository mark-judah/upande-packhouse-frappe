"""v15 -> v16 transactional data import: Stock Entry types Harvesting, Grading, Receiving.

WHY THIS EXISTS (see the design conversation for full context):
  - v16 introduced Farm/Business Unit as real Accounting Dimensions and moved cost
    centre resolution onto the Warehouse (Warehouse.custom_cost_center) instead of
    being set ad-hoc per transaction. v15's ~3.3M historical Harvesting/Grading/
    Receiving Stock Entries need re-mapping onto that shape, not a raw copy.
  - v15 and v16 are different companies (Karen Roses / KR vs Upande Farms Limited /
    UFL) on different sites, so every warehouse/cost-centre reference needs
    name-translation, not literal reuse.
  - Real v15 data confirms valuation is zero throughout these three entry types
    (allow_zero_valuation_rate is used consistently; total_incoming_value /
    total_outgoing_value / total_amount are 0.0 on every sampled record). That is
    what makes a fast, hand-rolled Stock Ledger Entry write safe here: the only
    real state to track is the running qty_after_transaction per (item, warehouse);
    valuation math is trivially zero, so there's no valuation queue to get wrong.
    ERPNext's own erpnext.stock.stock_ledger.make_sl_entries() was deliberately
    NOT used for the bulk write — it reposts the full valuation queue per call
    (update_entries_after), which is correct for a live system one voucher at a
    time but would make a 3.3M-row backfill take (by rough call-cost extrapolation)
    somewhere from many hours to multiple days rather than minutes.
  - GL Entries are intentionally NOT generated: with valuation_rate=0 throughout,
    every GL Entry would be a zero-amount no-op row — which matches what actually
    happened historically (the sampled real v15 records show zero GL impact), so
    skipping them is faithful to history, not a shortcut around it.

HOW IT RUNS (Frappe's System Console sandbox does not expose frappe.db.sql or
frappe.db.bulk_insert, and a synchronous console/HTTP request would time out long
before 3.3M records finish regardless of speed):
    A short snippet pasted into System Console calls the whitelisted
    `queue_import` below, which enqueues `run_import` as a background job. That
    job runs with NO sandbox restrictions (full frappe.db.sql / bulk_insert /
    requests), and can run for as long as the "long" queue's worker allows.

RESUMABILITY: progress is persisted to a JSON file (see _progress_path) after
every batch: source cursor (last v15 `creation` timestamp processed) per stock
entry type, counts, and the last error (if any). Re-running `queue_import` picks
up exactly where the previous run left off. The write side is also idempotent
independently of the cursor: every batch re-checks which v15 names already exist
in v16 (by name) before inserting, so a resume that re-fetches a already-imported
row is a safe no-op rather than a duplicate.
"""

import json
import re

import frappe
from frappe.utils import now_datetime

# ------------------------------------------------------------------
# Fixed mapping tables (built and verified against real data — see the
# design conversation for the schema comparison and record counts behind
# each of these).
# ------------------------------------------------------------------

SOURCE_COMPANY = "Karen Roses"
SOURCE_ABBR = "KR"

STOCK_ENTRY_TYPES = ["Harvesting", "Grading", "Receiving"]

# Only farms with real Harvesting/Grading/Receiving activity in v15 are listed
# (the other 9 records in v15's Farm doctype belong to other companies sharing
# that instance and never appear on a Karen Roses Stock Entry).
#
# Two named target profiles, because the two real targets so far are genuinely
# different shapes:
#   - "local": a fresh dev bench under a NEW company identity (Upande Farms
#     Limited / UFL) — warehouse/cost-centre names get built from scratch in a
#     "{Farm} GH {N} - UFL" convention (verified against real local data).
#   - "production": an in-place v16 upgrade of the SAME Karen Roses / KR
#     tenant v15 runs on. Company and warehouse names carry over UNCHANGED
#     (no "- KR" -> "- UFL" translation — it's the same company), farms are
#     already linked on most warehouses, and 212 Cost Centres already exist in
#     an existing "GH-NN {Farm} - KR" convention that must be matched, not
#     duplicated — see resolve_greenhouse's production branch.
TARGET_PROFILES = {
	"local": {
		"company": "Upande Farms Limited",
		"abbr": "UFL",
		"cost_center_style": "local",
		"cost_center_parent": "Upande Flowers Limited - UFL",
		"farm_map": {
			"Simotwo": "Simotwo",
			"Torongo": "Torongo",
			"Kapkolia": "Kapkolia",
			"Karen": "KAREN",
			"Chepsito": "Chepsito",
			"Kaptumbo": "Kaptumbo",
		},
	},
	"production": {
		"company": "Karen Roses",
		"abbr": "KR",
		"cost_center_style": "production",
		"cost_center_parent": None,  # resolved from the existing tree, see LookupCache
		"farm_map": {
			"Simotwo": "Simotwo",
			"Torongo": "Torongo",
			"Kapkolia": "Kapkolia",
			"Karen": "Karen",
			"Chepsito": "Chepsito",
			"Kaptumbo": "Kaptumbo",
		},
	},
}


def _farm_from_warehouse_prefix(v15_warehouse_name, farm_map):
	"""Last-resort farm hint for a non-greenhouse warehouse whose name still
	leads with the farm ("Simotwo Receiving Cold Store - KR", "Karen Graded
	Sold - KR") — used only when neither the Stock Entry header nor a
	greenhouse-pattern warehouse on the same row gave up a farm. Does NOT
	catch the hub-level names ("Ravine Available for Sale") — those still
	need the header/greenhouse to resolve, by design: a hub can hold more
	than one farm, so guessing from its name alone would be wrong."""
	if not v15_warehouse_name:
		return None
	for v15_farm, v16_farm in farm_map.items():
		if v15_warehouse_name.startswith(v15_farm + " "):
			return v16_farm
	return None

# v15 Cut Stage value -> v16 Cut Stage record name. Only "2.5-3.0" needed an
# actual rename; the rest already match 1:1 (Budwood and 3.5-4.0 were created
# fresh in v16 to close the gap — see the design conversation). Same map for
# every target — the Cut Stage master itself just needs the right records to
# exist on whichever site is being written to (see ensure_cut_stages).
CUT_STAGE_MAP = {
	"2.5-3.0": "2.5-3",
}
CUT_STAGE_VALUES_NEEDED = ["Budwood", "1.5-2.0", "2.0-2.5", "2.5-3", "3.5-4.0"]

# Matches "Torongo GH 12 - KR", "Torongo GH17 - KR" (real v15 data has both
# spaced and unspaced forms), case-insensitive on "GH". Source side only —
# v15 data is always "- KR" suffixed regardless of target.
GH_WAREHOUSE_RE = re.compile(r"^(.+?)\s+GH\s*0*(\d+)\s*-\s*" + SOURCE_ABBR + r"$", re.IGNORECASE)

# Three further real greenhouse-warehouse families (23,359 records combined)
# that don't fit GH_WAREHOUSE_RE's plain numbered pattern at all, each with
# its own cost-centre reuse rule (see resolve_greenhouse):
#  - IPM sub-greenhouses ("Chepsito GH IPM 01 - KR" etc, 6 warehouses) share
#    ONE cost centre across every farm/number ("IPM - KR" already exists),
#    not a per-number one.
#  - GH Tunnel ("Kapkolia  GH Tunnel - KR" — note the real double space, not
#    a typo to "fix", 1 warehouse but the single largest special case at
#    9,638 records) has its own dedicated per-farm cost centre, no number.
#  - Wetland GH Block ("Kapkolia Wetland  GH Block 01 - KR" — also a real
#    double space, 4 warehouses) has NO dedicated cost centre in the real
#    data, so falls back to the farm's own plain cost centre.
# Spellings below are copied verbatim from confirmed real v15/production
# names — v15 has exactly one spelling for each of these three (unlike the
# numbered pattern, which has both spaced/unspaced variants).
GH_IPM_RE = re.compile(r"^(.+?)\s+GH\s+IPM\s*0*(\d+)\s*-\s*" + SOURCE_ABBR + r"$", re.IGNORECASE)
GH_TUNNEL_RE = re.compile(r"^(.+?)\s+GH\s+Tunnel\s*-\s*" + SOURCE_ABBR + r"$", re.IGNORECASE)
GH_WETLAND_BLOCK_RE = re.compile(r"^(.+?)\s+Wetland\s+GH\s+Block\s*0*(\d+)\s*-\s*" + SOURCE_ABBR + r"$", re.IGNORECASE)

# Cost-centre naming convention already live on the production target — 212
# real records like "GH-19 Kapkolia - KR", "GH-01 Torongo - KR" (farm as a
# free-standing word, "GH" + number somewhere in the name; not always in the
# same order, and at least one is a known data-quality double-suffix typo —
# "Kapkolia GH18 - KR - KR"). Used to REUSE existing records instead of
# creating name-mismatched duplicates — see _build_production_cc_index.
_CC_GH_NUMBER_RE = re.compile(r"GH-?\s*0*(\d+)", re.IGNORECASE)


# ------------------------------------------------------------------
# Progress persistence
# ------------------------------------------------------------------

def _progress_path():
	return frappe.get_site_path("private", "files", "v15_stock_entry_import_progress.json")


def _load_progress():
	import os

	path = _progress_path()
	if not os.path.exists(path):
		return {t: {"cursor": "1900-01-01 00:00:00.000000", "imported": 0, "skipped": 0,
					"errors": 0, "done": False, "last_error": None} for t in STOCK_ENTRY_TYPES}
	with open(path) as f:
		data = json.load(f)
	for t in STOCK_ENTRY_TYPES:
		data.setdefault(t, {"cursor": "1900-01-01 00:00:00.000000", "imported": 0, "skipped": 0,
							 "errors": 0, "done": False, "last_error": None})
	return data


def _save_progress(progress):
	path = _progress_path()
	tmp = path + ".tmp"
	with open(tmp, "w") as f:
		json.dump(progress, f, indent=1, default=str)
	import os

	os.replace(tmp, path)  # atomic on the same filesystem — never leaves a half-written file


@frappe.whitelist()
def get_progress():
	"""Safe to call from the System Console sandbox directly (read-only)."""
	return _load_progress()


@frappe.whitelist()
def reset_progress(stock_entry_type=None):
	"""Danger: wipes the resume cursor. Does NOT delete already-imported records."""
	progress = _load_progress()
	types = [stock_entry_type] if stock_entry_type else STOCK_ENTRY_TYPES
	for t in types:
		progress[t] = {"cursor": "1900-01-01 00:00:00.000000", "imported": 0, "skipped": 0,
						"errors": 0, "done": False, "last_error": None}
	_save_progress(progress)
	return progress


# ------------------------------------------------------------------
# v15 HTTP pull (plain REST, NOT the FAC/MCP wrapper — that caps at 1000
# rows/call with JSON-RPC overhead per call; the plain /api/resource/ list
# endpoint tolerates page sizes in the thousands and is dramatically faster
# for bulk reads. Verified during the design conversation).
# ------------------------------------------------------------------

PARENT_FIELDS = [
	"name", "creation", "posting_date", "posting_time", "docstatus",
	"stock_entry_type", "purpose", "company",
	"custom_farm", "custom_location", "custom_greenhouse", "custom_business_unit",
	"custom_bucket_id", "custom_cut_stage", "custom_stem_length",
	"custom_harvester", "custom_graded_by", "custom_receiving_batch_id",
	"custom_scanned", "is_opening", "remarks",
]

CHILD_FIELDS = [
	"name", "parent", "idx", "item_code", "item_name", "qty", "uom", "stock_uom",
	"conversion_factor", "s_warehouse", "t_warehouse", "basic_rate",
]


class V15Client:
	def __init__(self, source_url, token):
		self.source_url = source_url.rstrip("/")
		self.token = token

	def _get(self, resource, params):
		import requests

		url = self.source_url + "/api/resource/" + resource
		headers = {"Authorization": "token " + self.token}
		resp = requests.get(url, headers=headers, params=params, timeout=120)
		resp.raise_for_status()
		return resp.json()["data"]

	def pull_parents(self, stock_entry_type, cursor, batch_size):
		params = {
			"filters": json.dumps([
				["stock_entry_type", "=", stock_entry_type],
				["company", "=", SOURCE_COMPANY],
				["creation", ">=", cursor],
			]),
			"fields": json.dumps(PARENT_FIELDS),
			"order_by": "creation asc, name asc",
			"limit_page_length": batch_size,
		}
		return self._get("Stock Entry", params)

	def pull_children(self, parent_names):
		if not parent_names:
			return {}
		params = {
			"parent": "Stock Entry",
			"filters": json.dumps([["parent", "in", parent_names]]),
			"fields": json.dumps(CHILD_FIELDS),
			"limit_page_length": 0,
		}
		rows = self._get("Stock Entry Detail", params)
		by_parent = {}
		for r in rows:
			by_parent.setdefault(r["parent"], []).append(r)
		for rows_ in by_parent.values():
			rows_.sort(key=lambda r: r.get("idx") or 0)
		return by_parent


# ------------------------------------------------------------------
# In-memory lookup caches, warmed once per job and reused for every batch —
# this is the "no N+1" half of the design: warehouse/cost-centre/employee/
# bucket resolution never re-queries per record.
# ------------------------------------------------------------------

def _build_production_cc_index(company, v16_farms):
	"""Index existing Cost Centres by (farm, greenhouse number) so the
	production target REUSES the 212 already there instead of creating
	name-mismatched duplicates. Real names don't follow one fixed template
	("GH-19 Kapkolia - KR", "GH-01 Torongo - KR", and at least one known typo
	"Kapkolia GH18 - KR - KR") — matched by finding a GH+number anywhere in
	the name and a known farm name as a free-standing word anywhere in it.
	When two existing records collide on the same (farm, number) key (as the
	typo does), the shorter name wins — the doubled "- KR - KR" suffix is
	reliably the longer one."""
	rows = frappe.get_all("Cost Center", filters={"company": company, "is_group": 0}, fields=["name"])
	index = {}
	for r in rows:
		name = r["name"]
		num_m = _CC_GH_NUMBER_RE.search(name)
		if not num_m:
			continue
		num = int(num_m.group(1))
		matched_farm = None
		for f in v16_farms:
			if re.search(r"\b" + re.escape(f) + r"\b", name, re.IGNORECASE):
				matched_farm = f
				break
		if not matched_farm:
			continue
		key = (matched_farm, num)
		if key not in index or len(name) < len(index[key]):
			index[key] = name
	return index


class LookupCache:
	def __init__(self, profile):
		self.profile = profile
		self.company = profile["company"]
		self.abbr = profile["abbr"]
		self.farm_map = profile["farm_map"]
		self.cost_center_style = profile["cost_center_style"]
		self._warehouse_cc = {}  # (farm_display, gh_num) -> (warehouse_name, cost_center_name)
		self._plain_warehouse_exists = {}  # v16 warehouse name -> bool
		self._employee_exists = set(frappe.get_all("Employee", pluck="name"))
		self._bucket_exists = set(frappe.get_all("Bucket QR Code", pluck="name"))
		self._new_buckets = set()  # created-this-run, not yet flushed to DB

		if profile["cost_center_style"] == "production":
			self._cc_index = _build_production_cc_index(self.company, set(self.farm_map.values()))
			self._cc_parent = frappe.db.get_value(
				"Cost Center", {"company": self.company, "is_group": 1, "parent_cost_center": ["is", "not set"]}
			)
		else:
			self._cc_index = {}
			self._cc_parent = profile["cost_center_parent"]

		self._default_inventory_account = self._resolve_default_inventory_account()

	def _resolve_default_inventory_account(self):
		"""Any newly-created plain Warehouse must carry a resolvable inventory
		Account — some ERPNext builds run Warehouse.validate_inventory_account()
		on save, which throws if neither the warehouse, its parent chain, nor
		the Company (Company.default_inventory_account) resolve one. Real data
		shows this company runs item-wise inventory accounting (every existing
		greenhouse Warehouse has account=None, resolved per-Item-Group instead),
		so we deliberately don't touch Company.default_inventory_account or try
		to satisfy the parent-chain inheritance — we just stamp a real, valid
		Stock account directly on the new Warehouse doc to clear the check.
		Preference order: Company's own default (if ever set) -> an Item
		Group's configured default_inventory_account for this company (the
		real per-item-group setup already in place) -> any Stock-type Account
		for this company -> None (creation will then throw, correctly, rather
		than silently posting to the wrong account)."""
		company_default = frappe.db.get_value("Company", self.company, "default_inventory_account")
		if company_default:
			return company_default
		item_group_default = frappe.db.get_value(
			"Item Default", {"parent": ["is", "set"], "company": self.company, "default_inventory_account": ["is", "set"]},
			"default_inventory_account",
		)
		if item_group_default:
			return item_group_default
		return frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Stock", "is_group": 0}, "name"
		)

	def _cc_name_for(self, v16_farm, gh_num):
		"""(cost_center_name, needs_create) in this profile's own convention."""
		if self.cost_center_style == "production":
			existing = self._cc_index.get((v16_farm, gh_num))
			if existing:
				return existing, False
			# Dry-run against the real 192 production Cost Centers showed the
			# dominant, numerically-majority naming convention is
			# "{Farm} GH{N} - {Abbr}" (no zero-padding, no dash) — e.g.
			# "Kapkolia GH19 - KR", "Chepsito GH3 - KR". A minority of older
			# records use "GH-{NN} {Farm} - {Abbr}"; that pattern is still
			# matched for lookup (via _CC_GH_NUMBER_RE) but is NOT what we
			# create new records as, to stay consistent with the majority.
			return "{0} GH{1} - {2}".format(v16_farm, gh_num, self.abbr), True
		return "{0} GH {1} - {2}".format(v16_farm, gh_num, self.abbr), True

	# -- greenhouse warehouse / cost centre (get-or-create in "local"; mostly
	#    reuse-only in "production", where the warehouses already exist) --
	def resolve_greenhouse(self, v15_warehouse):
		"""Returns (warehouse_name, cost_center_name, v16_farm_name) — the farm
		name is derived here, from the warehouse itself, rather than trusted
		from the Stock Entry header field (see _map_record for why).

		Tries the plain numbered pattern first, then the three special
		families (IPM / Tunnel / Wetland Block) — see the module-level
		GH_*_RE comments for what real data backs each one."""
		v15_warehouse = v15_warehouse or ""

		m = GH_WAREHOUSE_RE.match(v15_warehouse)
		if m:
			farm_display, gh_num = m.group(1).strip(), int(m.group(2))
			v16_farm = self.farm_map.get(farm_display, farm_display)
			# Always reconstruct the canonical name rather than trust v15's
			# raw string verbatim — real v15 data has inconsistent spacing
			# for the same greenhouse ("Torongo GH 17 - KR" vs "Torongo
			# GH17 - KR" both exist for the identical greenhouse), and
			# production's real warehouses all follow this exact canonical
			# "GH {NN}" zero-padded form regardless. Trusting the raw
			# string caused false "doesn't exist" existence checks and
			# doomed create-then-collide attempts.
			cc_name, needs_create_cc = self._cc_name_for(v16_farm, gh_num)
			return self._get_or_create_greenhouse(
				key=(farm_display, gh_num), v16_farm=v16_farm,
				warehouse_field_name="{0} GH {1:02d}".format(v16_farm, gh_num),
				cc_name=cc_name, needs_create_cc=needs_create_cc,
				cc_index_key=(v16_farm, gh_num),
			)

		m = GH_TUNNEL_RE.match(v15_warehouse)
		if m:
			farm_display = m.group(1).strip()
			v16_farm = self.farm_map.get(farm_display, farm_display)
			cc_name = "{0} GH Tunnel - {1}".format(v16_farm, self.abbr)
			return self._get_or_create_greenhouse(
				key=("__tunnel__", farm_display), v16_farm=v16_farm,
				# Real spelling has TWO spaces before "GH" — not a typo to
				# clean up, this is the actual live warehouse name.
				warehouse_field_name="{0}  GH Tunnel".format(v16_farm),
				cc_name=cc_name, needs_create_cc=not frappe.db.exists("Cost Center", cc_name),
			)

		m = GH_IPM_RE.match(v15_warehouse)
		if m:
			farm_display, gh_num = m.group(1).strip(), int(m.group(2))
			v16_farm = self.farm_map.get(farm_display, farm_display)
			# One cost centre shared across every farm/number — "IPM - KR"
			# already exists in real data as a single, non-farm-specific
			# record, not a per-number series like the plain GH pattern.
			cc_name = "IPM - {0}".format(self.abbr)
			return self._get_or_create_greenhouse(
				key=("__ipm__", farm_display, gh_num), v16_farm=v16_farm,
				warehouse_field_name="{0} GH IPM {1:02d}".format(v16_farm, gh_num),
				cc_name=cc_name, needs_create_cc=not frappe.db.exists("Cost Center", cc_name),
			)

		m = GH_WETLAND_BLOCK_RE.match(v15_warehouse)
		if m:
			farm_display, gh_num = m.group(1).strip(), int(m.group(2))
			v16_farm = self.farm_map.get(farm_display, farm_display)
			# No dedicated cost centre exists for this family in real data —
			# fall back to the farm's own plain cost centre, and never
			# create one here (a missing farm-level cost centre is a
			# deeper gap than this migration should paper over).
			cc_name = "{0} - {1}".format(v16_farm, self.abbr)
			return self._get_or_create_greenhouse(
				key=("__wetland_block__", farm_display, gh_num), v16_farm=v16_farm,
				# Real spelling has TWO spaces before "GH".
				warehouse_field_name="{0} Wetland  GH Block {1:02d}".format(v16_farm, gh_num),
				cc_name=cc_name if frappe.db.exists("Cost Center", cc_name) else None,
				needs_create_cc=False,
			)

		return None, None, None

	def _get_or_create_greenhouse(self, key, v16_farm, warehouse_field_name, cc_name, needs_create_cc, cc_index_key=None):
		"""Shared get-or-create body for every greenhouse-warehouse family
		resolve_greenhouse dispatches to. warehouse_field_name is the
		Warehouse.warehouse_name value (pre-autoname, i.e. without the
		"- {abbr}" suffix); the actual Warehouse.name is that plus the
		suffix, via Frappe's own Warehouse.autoname()."""
		if key in self._warehouse_cc:
			wh, cc = self._warehouse_cc[key]
			return wh, cc, v16_farm

		warehouse_name = "{0} - {1}".format(warehouse_field_name, self.abbr)

		if not frappe.db.exists("Warehouse", warehouse_name):
			parent_wh = frappe.db.get_value(
				"Warehouse", {"warehouse_name": v16_farm, "is_group": 1, "company": self.company}
			) or frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1, "name": ["like", "%" + self.abbr]})
			frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": warehouse_field_name,
				"company": self.company,
				"is_group": 0,
				"parent_warehouse": parent_wh,
				"custom_farm": v16_farm if frappe.db.exists("Farm", v16_farm) else None,
				"account": self._default_inventory_account,
			}).insert(ignore_permissions=True)

		if needs_create_cc and cc_name and not frappe.db.exists("Cost Center", cc_name):
			frappe.get_doc({
				"doctype": "Cost Center",
				"cost_center_name": cc_name[: -len(" - " + self.abbr)] if cc_name.endswith(" - " + self.abbr) else cc_name,
				"company": self.company,
				"parent_cost_center": self._cc_parent,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			if cc_index_key:
				self._cc_index[cc_index_key] = cc_name

		if cc_name and frappe.db.get_value("Warehouse", warehouse_name, "custom_cost_center") != cc_name:
			frappe.db.set_value("Warehouse", warehouse_name, "custom_cost_center", cc_name)

		self._warehouse_cc[key] = (warehouse_name, cc_name)
		return warehouse_name, cc_name, v16_farm

	def resolve_warehouse(self, v15_warehouse, record_farm=None):
		"""Any warehouse reference on an item row — greenhouse, an exact-match
		plain warehouse (Rejects, Quarantined etc), or one of v15's many
		"stock is graded and ready to sell" warehouses (Available for Sale /
		Graded Sold / Packhouse Store — per-farm, per-location/hub, AND
		several old abbreviated names like "KAPK Available for Sale" all
		coexist in the real v15 data). v16 collapsed that whole family onto
		one thing: the farm's own Receiving Cold Store — same convention
		already used throughout this app (see e.g. warehouse_for() in
		upande_quality's mobile api). Since these warehouse names don't
		reliably carry the farm themselves (a hub-level "Ravine Available for
		Sale" is shared by several farms), the record's OWN already-resolved
		farm (from custom_greenhouse) is what routes it, not the warehouse
		name.

		Returns (warehouse_name, cost_center_name); cost_center_name is None
		for plain/collapsed warehouses (they don't drive the greenhouse
		cost-centre hook and keep whatever cost centre the item otherwise
		resolves from)."""
		if not v15_warehouse:
			return None, None
		wh, cc, _farm = self.resolve_greenhouse(v15_warehouse)
		if wh:
			return wh, cc
		translated = self._translate_warehouse(v15_warehouse)
		if translated not in self._plain_warehouse_exists:
			self._plain_warehouse_exists[translated] = bool(frappe.db.exists("Warehouse", translated))
		if self._plain_warehouse_exists[translated]:
			return translated, None
		if record_farm:
			return self.resolve_farm_cold_store(record_farm), None
		return None, None

	def _translate_warehouse(self, v15_name):
		"""production: same company, name is unchanged. local: "- KR" -> "- UFL"."""
		if self.cost_center_style == "production":
			return v15_name
		if v15_name and v15_name.endswith(" - " + SOURCE_ABBR):
			return v15_name[: -len(" - " + SOURCE_ABBR)] + " - " + self.abbr
		return v15_name

	def resolve_farm_cold_store(self, v16_farm):
		key = ("__cold_store__", v16_farm)
		if key in self._warehouse_cc:
			return self._warehouse_cc[key][0]
		name = "{0} Receiving Cold Store - {1}".format(v16_farm, self.abbr)
		if not frappe.db.exists("Warehouse", name):
			parent_wh = frappe.db.get_value("Warehouse", {"warehouse_name": v16_farm, "is_group": 1, "company": self.company}) \
				or frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 1, "name": ["like", "%" + self.abbr]})
			frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "{0} Receiving Cold Store".format(v16_farm),
				"company": self.company,
				"is_group": 0,
				"parent_warehouse": parent_wh,
				"custom_farm": v16_farm if frappe.db.exists("Farm", v16_farm) else None,
				"account": self._default_inventory_account,
			}).insert(ignore_permissions=True)
		self._warehouse_cc[key] = (name, None)
		return name

	def resolve_farm(self, v15_farm):
		return self.farm_map.get(v15_farm)

	def resolve_cut_stage(self, v15_value):
		if not v15_value:
			return None
		return CUT_STAGE_MAP.get(v15_value, v15_value)

	def resolve_employee(self, employee_id):
		if employee_id and employee_id in self._employee_exists:
			return employee_id
		return None

	def ensure_buckets(self, bucket_ids):
		"""Batch get-or-create for Bucket QR Code — one existence check + one
		bulk_insert per batch, not per record."""
		missing = [b for b in bucket_ids if b and b not in self._bucket_exists and b not in self._new_buckets]
		if not missing:
			return
		docs = []
		for bid in missing:
			doc = frappe.new_doc("Bucket QR Code")
			doc.id = bid
			doc.status = "Available"
			doc.name = bid
			docs.append(doc)
			self._new_buckets.add(bid)
		from frappe.model.document import bulk_insert as _bulk_insert

		_bulk_insert("Bucket QR Code", docs, ignore_duplicates=True)
		self._bucket_exists.update(missing)


@frappe.whitelist()
def ensure_cut_stages():
	"""Create whichever of CUT_STAGE_VALUES_NEEDED don't exist yet on this
	site. Safe to call from System Console directly (small, synchronous) —
	run once per target before the first real import batch."""
	created = []
	for val in CUT_STAGE_VALUES_NEEDED:
		if not frappe.db.exists("Cut Stage", val):
			frappe.get_doc({"doctype": "Cut Stage", "cutstage": val}).insert(ignore_permissions=True)
			created.append(val)
	frappe.db.commit()
	return {"created": created, "already_present": [v for v in CUT_STAGE_VALUES_NEEDED if v not in created]}


# ------------------------------------------------------------------
# Running per-(item, warehouse) stock balance. Records are pulled and
# processed in ascending `creation` order, so a plain running total is
# correct without needing ERPNext's full repost machinery — see the module
# docstring for why that machinery was deliberately not used here.
# ------------------------------------------------------------------

class RunningBalance:
	def __init__(self):
		self._balances = {}

	def apply(self, item_code, warehouse, actual_qty):
		key = (item_code, warehouse)
		if key not in self._balances:
			existing = frappe.db.get_value(
				"Stock Ledger Entry",
				{"item_code": item_code, "warehouse": warehouse, "is_cancelled": 0},
				"qty_after_transaction",
				order_by="posting_date desc, posting_time desc, creation desc",
			)
			self._balances[key] = float(existing or 0)
		self._balances[key] += actual_qty
		return self._balances[key]


# ------------------------------------------------------------------
# Mapping: one v15 (parent, items) pair -> a v16 Stock Entry Document (with
# items attached) + the Stock Ledger Entry rows it implies. Returns
# (doc_or_None, sle_rows, skip_reason_or_None).
# ------------------------------------------------------------------

def _v16_name(v15_name):
	if v15_name.startswith("SE-"):
		return "MAT-STE-" + v15_name[len("SE-"):]
	return v15_name


def _map_record(parent, items, cache, balances, fiscal_year_cache):
	gh_warehouse = cc = gh_farm = None
	if parent.get("custom_greenhouse"):
		gh_warehouse, cc, gh_farm = cache.resolve_greenhouse(parent["custom_greenhouse"])
		if not gh_warehouse:
			return None, [], "unrecognised greenhouse warehouse: {0}".format(parent["custom_greenhouse"])

	# The greenhouse warehouse name is the authoritative source for which farm
	# this actually is — real v15 data has records where custom_farm (the
	# header field, entered/derived separately) disagrees with the farm
	# embedded in custom_greenhouse's own warehouse name (e.g. header says
	# "Simotwo" while custom_greenhouse is a Torongo warehouse). Trust the
	# warehouse; fall back to the header field only when there's no
	# greenhouse to derive it from (some Receiving entries target a plain
	# cold-store warehouse instead).
	farm = gh_farm or cache.resolve_farm(parent.get("custom_farm"))
	if parent.get("custom_farm") and not gh_farm and not farm:
		return None, [], "unknown farm: {0}".format(parent.get("custom_farm"))

	v16_name = _v16_name(parent["name"])

	doc = frappe.new_doc("Stock Entry")
	doc.name = v16_name
	doc.stock_entry_type = parent["stock_entry_type"]
	doc.purpose = parent.get("purpose") or "Material Receipt"
	doc.company = cache.company
	doc.posting_date = parent["posting_date"]
	doc.posting_time = parent["posting_time"]
	doc.set_posting_time = 1
	doc.docstatus = 1
	doc.is_opening = parent.get("is_opening") or "No"
	doc.farm = farm
	doc.custom_business_unit = "Roses"
	doc.business_unit = "Roses"
	doc.custom_greenhouse = gh_warehouse
	doc.cost_center = cc
	doc.custom_bucket_id = parent.get("custom_bucket_id") or None
	doc.custom_cut_stage = cache.resolve_cut_stage(parent.get("custom_cut_stage"))
	doc.custom_stem_length = (parent.get("custom_stem_length") or "").upper() or None
	doc.custom_harvester = cache.resolve_employee(parent.get("custom_harvester"))
	doc.custom_graded_by = cache.resolve_employee(parent.get("custom_graded_by"))
	doc.custom_receiving_batch_id = parent.get("custom_receiving_batch_id") or None
	doc.remarks = "Migrated from v15 {0}".format(parent["name"])

	fy_key = str(parent["posting_date"])
	if fy_key not in fiscal_year_cache:
		try:
			from erpnext.accounts.utils import get_fiscal_year

			fiscal_year_cache[fy_key] = get_fiscal_year(parent["posting_date"], company=cache.company)[0]
		except Exception:
			fiscal_year_cache[fy_key] = None
	fiscal_year = fiscal_year_cache[fy_key]

	sle_rows = []
	mapped_items = []
	for it in items:
		# Some Grading/Receiving records carry no farm on the header at all —
		# the only place the real farm shows up is embedded in one side's own
		# greenhouse warehouse name (e.g. s_warehouse = "Torongo GH 12 - KR",
		# t_warehouse = the ambiguous hub-level "Karen Available for Sale").
		# Resolve whichever side is a greenhouse FIRST so its farm can back-fill
		# the other, ambiguous side — and the doc header itself, if it was
		# never set from custom_greenhouse/custom_farm above.
		_, _, s_gh_farm = cache.resolve_greenhouse(it.get("s_warehouse")) if it.get("s_warehouse") else (None, None, None)
		_, _, t_gh_farm = cache.resolve_greenhouse(it.get("t_warehouse")) if it.get("t_warehouse") else (None, None, None)
		effective_farm = (
			farm or s_gh_farm or t_gh_farm
			or _farm_from_warehouse_prefix(it.get("s_warehouse"), cache.farm_map)
			or _farm_from_warehouse_prefix(it.get("t_warehouse"), cache.farm_map)
		)
		if not farm and effective_farm:
			farm = effective_farm
			doc.farm = farm

		s_wh, _ = cache.resolve_warehouse(it.get("s_warehouse"), record_farm=effective_farm) if it.get("s_warehouse") else (None, None)
		t_wh, item_cc = cache.resolve_warehouse(it.get("t_warehouse"), record_farm=effective_farm) if it.get("t_warehouse") else (None, None)
		if it.get("s_warehouse") and not s_wh:
			return None, [], "unrecognised source warehouse: {0}".format(it.get("s_warehouse"))
		if it.get("t_warehouse") and not t_wh:
			return None, [], "unrecognised target warehouse: {0}".format(it.get("t_warehouse"))

		row_cc = item_cc or cc
		row = {
			"doctype": "Stock Entry Detail",
			# bulk_insert() does no due-diligence at all (see its own docstring) —
			# it will not autoname child rows, so every one needs an explicit
			# unique name or it's silently dropped from the multi-row insert.
			"name": frappe.generate_hash(length=10),
			"item_code": it["item_code"],
			"item_name": it.get("item_name") or it["item_code"],
			"qty": it.get("qty") or 0,
			"transfer_qty": it.get("qty") or 0,
			"uom": it.get("uom") or "Stems",
			"stock_uom": it.get("stock_uom") or it.get("uom") or "Stems",
			"conversion_factor": it.get("conversion_factor") or 1,
			"s_warehouse": s_wh,
			"t_warehouse": t_wh,
			"cost_center": row_cc,
			"business_unit": "Roses",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 1,
		}
		child = doc.append("items", row)
		mapped_items.append(child)

		qty = float(it.get("qty") or 0)
		if qty <= 0:
			continue
		posting_dt = "{0} {1}".format(parent["posting_date"], parent["posting_time"])
		if s_wh:
			bal = balances.apply(it["item_code"], s_wh, -qty)
			sle_rows.append(_build_sle_row(it["item_code"], s_wh, -qty, bal, posting_dt, parent, v16_name, fiscal_year, cache.company))
		if t_wh:
			bal = balances.apply(it["item_code"], t_wh, qty)
			sle_rows.append(_build_sle_row(it["item_code"], t_wh, qty, bal, posting_dt, parent, v16_name, fiscal_year, cache.company))

	return doc, sle_rows, None


def _build_sle_row(item_code, warehouse, actual_qty, qty_after, posting_dt, parent, voucher_no, fiscal_year, company):
	return {
		"doctype": "Stock Ledger Entry",
		"name": frappe.generate_hash(length=10),
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": parent["posting_date"],
		"posting_time": parent["posting_time"],
		"posting_datetime": posting_dt,
		"voucher_type": "Stock Entry",
		"voucher_no": voucher_no,
		"actual_qty": actual_qty,
		"qty_after_transaction": qty_after,
		"incoming_rate": 0,
		"outgoing_rate": 0,
		"valuation_rate": 0,
		"stock_value": 0,
		"stock_value_difference": 0,
		"company": company,
		"fiscal_year": fiscal_year,
		"is_cancelled": 0,
		"docstatus": 1,
	}


# ------------------------------------------------------------------
# Batch write — the actual "no N+1" bulk insert.
# ------------------------------------------------------------------

def _write_batch(docs, sle_rows):
	if not docs:
		return 0, 0
	names = [d.name for d in docs]
	existing = set(frappe.db.sql(
		"SELECT name FROM `tabStock Entry` WHERE name IN ({0})".format(", ".join(["%s"] * len(names))),
		names, pluck=True,
	))
	new_docs = [d for d in docs if d.name not in existing]
	skipped = len(docs) - len(new_docs)
	if not new_docs:
		return 0, skipped

	from frappe.model.document import bulk_insert as _bulk_insert

	for doc in new_docs:
		doc.creation = doc.modified = now_datetime()
		doc.owner = doc.modified_by = frappe.session.user
	_bulk_insert("Stock Entry", new_docs, ignore_duplicates=True)

	if sle_rows:
		new_names = {d.name for d in new_docs}
		sle_docs = []
		for row in sle_rows:
			if row["voucher_no"] not in new_names:
				continue  # SLE for a Stock Entry that turned out to already exist — skip, its SLE exists too
			sle_doc = frappe.new_doc("Stock Ledger Entry")
			sle_doc.update(row)
			sle_docs.append(sle_doc)
		if sle_docs:
			_bulk_insert("Stock Ledger Entry", sle_docs, ignore_duplicates=True)

	return len(new_docs), skipped


# ------------------------------------------------------------------
# Main resumable loop.
# ------------------------------------------------------------------

@frappe.whitelist()
def run_import(source_url, token, stock_entry_type=None, batch_size=2000, time_budget_seconds=1200,
		target_profile="local", max_batches=None):
	"""The real engine. Runs unrestricted (no System Console sandbox) once picked
	up by a background worker. Processes batches until either everything is
	caught up or `time_budget_seconds` is spent, then re-enqueues itself to
	continue — so one trigger keeps the whole 3.3M-row backfill moving without
	needing to be re-run by hand, while never risking more than one batch's
	progress on a crash (progress is saved after every batch).

	target_profile: "local" (fresh Upande Farms Limited / UFL dev bench, the
	default) or "production" (in-place v16 upgrade of the real Karen Roses / KR
	tenant — same company as the source, existing warehouses/cost centres get
	reused by name/pattern rather than recreated). See TARGET_PROFILES.

	max_batches: safety cap for validation runs — process at most this many
	batches THEN STOP (no self-requeue), leaving the cursor exactly where it
	is so a full run can resume later. None (default) = unlimited, the normal
	production-backfill behaviour. Always pass this explicitly for a first
	test against a target you haven't run before."""
	import time

	if target_profile not in TARGET_PROFILES:
		raise frappe.ValidationError("Unknown target_profile {0!r} — must be one of {1}".format(
			target_profile, list(TARGET_PROFILES)))
	profile = TARGET_PROFILES[target_profile]

	batch_size = int(batch_size)
	time_budget_seconds = int(time_budget_seconds)
	max_batches = int(max_batches) if max_batches not in (None, "", "None") else None
	types = [stock_entry_type] if stock_entry_type else STOCK_ENTRY_TYPES

	client = V15Client(source_url, token)
	cache = LookupCache(profile)
	balances = RunningBalance()
	fiscal_year_cache = {}
	progress = _load_progress()
	started = time.monotonic()
	batches_done = 0

	for t in types:
		if progress[t]["done"]:
			continue
		while True:
			if max_batches is not None and batches_done >= max_batches:
				# Validation-run cap reached — stop cleanly, no requeue. Cursor
				# is untouched from the last successful batch, so a later call
				# (with or without a cap) resumes exactly here.
				return progress

			if time.monotonic() - started > time_budget_seconds:
				_requeue(source_url, token, stock_entry_type, batch_size, time_budget_seconds, target_profile)
				return progress

			cursor = progress[t]["cursor"]
			try:
				parents = client.pull_parents(t, cursor, batch_size)
			except Exception as e:
				progress[t]["errors"] += 1
				progress[t]["last_error"] = "pull failed: {0}".format(e)
				_save_progress(progress)
				if max_batches is None:
					_requeue(source_url, token, stock_entry_type, batch_size, time_budget_seconds, target_profile, delay=60)
				return progress

			if not parents:
				progress[t]["done"] = True
				_save_progress(progress)
				break

			try:
				children_by_parent = client.pull_children([p["name"] for p in parents])
			except Exception as e:
				progress[t]["errors"] += 1
				progress[t]["last_error"] = "child pull failed: {0}".format(e)
				_save_progress(progress)
				if max_batches is None:
					_requeue(source_url, token, stock_entry_type, batch_size, time_budget_seconds, target_profile, delay=60)
				return progress

			cache.ensure_buckets([p.get("custom_bucket_id") for p in parents])

			docs, sle_rows = [], []
			batch_errors = 0
			for p in parents:
				doc, sles, reason = _map_record(p, children_by_parent.get(p["name"], []), cache, balances, fiscal_year_cache)
				if reason:
					batch_errors += 1
					progress[t]["last_error"] = "{0}: {1}".format(p["name"], reason)
					continue
				docs.append(doc)
				sle_rows.extend(sles)

			try:
				inserted, skipped = _write_batch(docs, sle_rows)
				frappe.db.commit()
			except Exception as e:
				frappe.db.rollback()
				progress[t]["errors"] += 1
				progress[t]["last_error"] = "write failed: {0}".format(e)
				_save_progress(progress)
				if max_batches is None:
					_requeue(source_url, token, stock_entry_type, batch_size, time_budget_seconds, target_profile, delay=60)
				return progress

			progress[t]["imported"] += inserted
			progress[t]["skipped"] += skipped
			progress[t]["errors"] += batch_errors
			progress[t]["cursor"] = parents[-1]["creation"]
			_save_progress(progress)
			batches_done += 1

			frappe.publish_progress(
				percent=None,
				title="v15 import — {0}".format(t),
				description="{0} imported, {1} skipped, {2} errors so far".format(
					progress[t]["imported"], progress[t]["skipped"], progress[t]["errors"]
				),
			)

			if len(parents) < batch_size:
				# short page = caught up to the live tail; loop again next
				# tick rather than spinning immediately.
				progress[t]["done"] = True
				_save_progress(progress)
				break

	return progress


def _requeue(source_url, token, stock_entry_type, batch_size, time_budget_seconds, target_profile, delay=5):
	frappe.enqueue(
		"upande_packhouse.migrations.v15_import.run_import",
		queue="long",
		timeout=time_budget_seconds + 300,
		enqueue_after_commit=True,
		source_url=source_url,
		token=token,
		stock_entry_type=stock_entry_type,
		batch_size=batch_size,
		time_budget_seconds=time_budget_seconds,
		target_profile=target_profile,
	)


@frappe.whitelist()
def queue_import(source_url, token, stock_entry_type=None, batch_size=2000, time_budget_seconds=1200,
		target_profile="local", max_batches=None):
	"""Entry point safe to paste into System Console. Kicks off the background
	job chain and returns immediately — check progress with get_progress().
	target_profile: "local" or "production" — see TARGET_PROFILES / run_import.
	max_batches: pass this for any first/validation run against a target —
	see run_import's docstring. Leave unset only for the real full backfill."""
	if target_profile not in TARGET_PROFILES:
		raise frappe.ValidationError("Unknown target_profile {0!r} — must be one of {1}".format(
			target_profile, list(TARGET_PROFILES)))
	frappe.enqueue(
		"upande_packhouse.migrations.v15_import.run_import",
		queue="long",
		timeout=int(time_budget_seconds) + 300,
		source_url=source_url,
		token=token,
		stock_entry_type=stock_entry_type,
		batch_size=batch_size,
		time_budget_seconds=time_budget_seconds,
		target_profile=target_profile,
		max_batches=max_batches,
	)
	return "Queued. Call upande_packhouse.migrations.v15_import.get_progress() to check status."
