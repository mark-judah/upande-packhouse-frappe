# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""The six label types, their QR payloads and their default designs.

The payloads are what the scanner apps already parse (upande-packhouse and
upande-quality ScanField consumers); they are fixed per type and never part of
a template, so a design change can never make a label unreadable:

    bunch    {"bunch_id":"BUNCH-123"}
    bucket   {"A3F09C":"bucket"}            the id is the KEY
    shelf    {"shelf":"KPK-D21M"}
    sf_shelf {"shelf":"SF-A1T"}             sea-freight cold room, same shape
    trolley  {"KPK-TROLLEY-1":"trolley"}    the id is the KEY
    staging  {"location":"COLD ROOM 2"}

Compact JSON on purpose: the scanner's ScanField treats a buffer that is valid
JSON ending in "}" as "scan complete", so every payload must be JSON, and a
shorter payload is a smaller, easier QR.
"""

import json

PAGE_PRESETS = {
	"69x100": (69, 100),
	"100x69": (100, 69),
	"160x40": (160, 40),
	"100x50": (100, 50),
	"50x30": (50, 30),
}

TYPES = {
	"bunch": {
		"label": "Bunch",
		"kind": "sequence",
		"legacy": "Bunch QR Code",
		"fields": ["code", "number", "variety", "bunch_size", "stem_length", "farm", "farm_code"],
		"sample": {
			"code": "BUNCH-127038",
			"number": "127038",
			"variety": "Marisa",
			"bunch_size": "Bunch (10)",
			"stem_length": "62cm",
			"farm": "Kapkolia",
			"farm_code": "KE/030/0809/047",
		},
	},
	"bucket": {
		"label": "Bucket",
		"kind": "random",
		"legacy": "Bucket QR Code",
		"fields": ["code"],
		"sample": {"code": "A3F09C"},
	},
	"shelf": {
		"label": "Shelf",
		"kind": "fixed",
		"legacy": "Shelf QR Code",
		"fields": ["code", "prefix", "main", "row", "pos", "level"],
		"sample": {"code": "KPK-D21M", "prefix": "KPK", "main": "D21M", "row": "D", "pos": "21", "level": "M"},
	},
	"sf_shelf": {
		"label": "Sea freight shelf",
		"kind": "fixed",
		"legacy": "Shelf QR Code",
		"fields": ["code", "prefix", "main", "row", "pos", "level"],
		"sample": {"code": "SF-A16T", "prefix": "SF", "main": "A16T", "row": "A", "pos": "16", "level": "T"},
	},
	"trolley": {
		"label": "Trolley",
		"kind": "fixed",
		"legacy": None,
		"fields": ["code", "prefix", "number"],
		"sample": {"code": "KPK-TROLLEY-1", "prefix": "KPK", "number": "1"},
	},
	"staging": {
		"label": "Staging location",
		"kind": "fixed",
		"legacy": None,
		"fields": ["code"],
		"sample": {"code": "COLD ROOM 2"},
	},
}


def payload(label_type, code):
	if label_type == "bunch":
		return json.dumps({"bunch_id": code}, separators=(",", ":"))
	if label_type == "bucket":
		return json.dumps({code: "bucket"}, separators=(",", ":"))
	if label_type in ("shelf", "sf_shelf"):
		return json.dumps({"shelf": code}, separators=(",", ":"))
	if label_type == "trolley":
		return json.dumps({code: "trolley"}, separators=(",", ":"))
	if label_type == "staging":
		return json.dumps({"location": code}, separators=(",", ":"), ensure_ascii=False)
	raise KeyError(label_type)


def _line(text, scale=1.0, bold=True, upper=False, size=0):
	return {"text": text, "scale": scale, "bold": bold, "upper": upper, "size": size}


def _design(page, margin, qr_size, position, lines, gap=4.0, line_gap=0.3, text_align="left", align="center", group="center"):
	return {
		"page": {"w": page[0], "h": page[1]},
		"margin": margin,
		"qr": {"size": qr_size, "ec": "Q"},
		"layout": {
			"text_position": position,
			"gap": gap,
			"align": align,
			"text_align": text_align,
			"group": group,
			"line_gap": line_gap,
		},
		"lines": lines,
		"max_pt": 96,
	}


_SHELF_LINES = [_line("{prefix}", 0.326), _line("{main}", 1.0)]

DEFAULT_DESIGNS = {
	"bunch": _design(
		(160, 40),
		3,
		0,
		"right",
		[_line("{variety}"), _line("{bunch_size}"), _line("{stem_length}"), _line("{farm_code}")],
		gap=4,
		line_gap=0.35,
		group="start",
	),
	"bucket": _design((69, 100), 4, 0, "top", [_line("{code}", upper=True)], gap=2, text_align="center"),
	"shelf": _design((100, 69), 5, 30, "right", _SHELF_LINES, gap=5, line_gap=0.27),
	"sf_shelf": _design((100, 69), 5, 30, "right", _SHELF_LINES, gap=5, line_gap=0.27),
	"trolley": _design(
		(100, 69),
		5,
		30,
		"right",
		[_line("{prefix}", 0.326), _line("TROLLEY {number}", 1.0)],
		gap=5,
		line_gap=0.27,
	),
	"staging": _design((100, 69), 5, 0, "bottom", [_line("{code}", upper=True)], gap=3, text_align="center"),
}
