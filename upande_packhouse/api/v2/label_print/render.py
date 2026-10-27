# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt

"""Label geometry and PDF output.

Why a hand-written PDF writer: a label is a few hundred filled rectangles (the
QR modules, merged into horizontal runs) plus a few text strings. Writing those
straight into a PDF content stream costs about the QR matrix itself (~3 ms a
label); building the same page through a PDF library cost ~10 ms. For a
7,000-label run that is the difference between half a minute and over a minute,
and the QR is vector, so it is sharp at any printer resolution.

Layout is solved ONCE for a whole batch (`solve`), from the widest value of
every text line, so every label in the batch has identical geometry and a row of
printed labels lines up -- the same rule the shelf script used.
"""

import base64
import io
import zlib

import frappe
import pymupdf
import qrcode
from frappe import _

from upande_packhouse.api.v2.label_print import types as lp_types

MM = 72.0 / 25.4
CAP = 0.718  # Helvetica cap height, em
FONT = {False: ("F1", "helv"), True: ("F2", "hebo")}
MIN_PT = 4.0
EC = {
	"L": qrcode.constants.ERROR_CORRECT_L,
	"M": qrcode.constants.ERROR_CORRECT_M,
	"Q": qrcode.constants.ERROR_CORRECT_Q,
	"H": qrcode.constants.ERROR_CORRECT_H,
}
PAPER = {"A4": (210.0, 297.0), "A5": (148.0, 210.0), "Letter": (215.9, 279.4)}


# ---------------------------------------------------------------- design


def _num(v, lo, hi, default):
	try:
		v = float(v)
	except (TypeError, ValueError):
		return default
	return max(lo, min(hi, v))


def normalize_design(d, label_type):
	"""A design that is always safe to render: unknown keys dropped, numbers
	clamped, placeholders limited to the type's own fields."""
	base = lp_types.DEFAULT_DESIGNS[label_type]
	d = d or {}
	page = d.get("page") or base["page"]
	qr = d.get("qr") or {}
	lay = d.get("layout") or {}
	fields = set(lp_types.TYPES[label_type]["fields"])

	lines = []
	for ln in (d.get("lines") if d.get("lines") is not None else base["lines"])[:8]:
		text = str(ln.get("text") or "")[:80]
		lines.append(
			{
				"text": text,
				"scale": _num(ln.get("scale"), 0.1, 3, 1.0),
				"bold": bool(ln.get("bold", True)),
				"upper": bool(ln.get("upper", False)),
				"size": _num(ln.get("size"), 0, 200, 0),
			}
		)

	out = {
		"page": {"w": _num(page.get("w"), 15, 400, 100), "h": _num(page.get("h"), 10, 400, 69)},
		"margin": _num(d.get("margin"), 0, 30, base["margin"]),
		"qr": {
			"size": _num(qr.get("size"), 0, 300, 0),
			"ec": qr.get("ec") if qr.get("ec") in EC else "Q",
		},
		"layout": {
			"text_position": lay.get("text_position")
			if lay.get("text_position") in ("left", "right", "top", "bottom")
			else base["layout"]["text_position"],
			"gap": _num(lay.get("gap"), 0, 50, base["layout"]["gap"]),
			"align": lay.get("align") if lay.get("align") in ("start", "center", "end") else "center",
			"text_align": lay.get("text_align")
			if lay.get("text_align") in ("left", "center", "right")
			else "left",
			"group": lay.get("group") if lay.get("group") in ("start", "center", "end") else "center",
			"line_gap": _num(lay.get("line_gap"), 0, 2, base["layout"]["line_gap"]),
		},
		"lines": lines,
		"max_pt": _num(d.get("max_pt"), 6, 300, 96),
		"base_pt": _num(d.get("base_pt"), 0, 300, 0),
	}
	out["_unknown_fields"] = sorted({f for ln in lines for f in _placeholders(ln["text"]) if f not in fields})
	return out


def _placeholders(text):
	import re

	return re.findall(r"\{(\w+)\}", text or "")


def fill(text, ctx, upper=False):
	out = text
	for k, v in ctx.items():
		out = out.replace("{" + k + "}", "" if v is None else str(v))
	return out.upper() if upper else out


# ---------------------------------------------------------------- QR


def qr_runs(payload, ec="Q"):
	"""(runs, modules): horizontal runs of dark modules as (x, y, length)."""
	q = qrcode.QRCode(error_correction=EC[ec], border=0)
	q.add_data(payload)
	q.make(fit=True)
	m = q.get_matrix()
	runs = []
	for y, row in enumerate(m):
		x, n = 0, len(row)
		while x < n:
			if row[x]:
				s = x
				while x < n and row[x]:
					x += 1
				runs.append((s, y, x - s))
			else:
				x += 1
	return runs, len(m)


# ---------------------------------------------------------------- geometry


def _tw(text, bold, size):
	return pymupdf.get_text_length(text, fontname=FONT[bool(bold)][1], fontsize=size)


def solve(design, ctxs, label_type):
	"""Geometry shared by every label whose values are in `ctxs`."""
	ctxs = ctxs or [dict(lp_types.TYPES[label_type]["sample"])]
	W, H = design["page"]["w"] * MM, design["page"]["h"] * MM
	m = design["margin"] * MM
	lay = design["layout"]
	lines = design["lines"]
	pos = lay["text_position"]
	gap = lay["gap"] * MM
	has_text = any(ln["text"].strip() for ln in lines)
	warnings = []

	ax, ay, aw, ah = m, m, W - 2 * m, H - 2 * m
	if aw <= 10 or ah <= 10:
		frappe.throw(_("The margin leaves no room on the label."))
	horizontal = pos in ("left", "right")

	# widest value of each line, in em
	unit = []
	for ln in lines:
		w = 0.0
		seen = set()
		for ctx in ctxs:
			s = fill(ln["text"], ctx, ln["upper"])
			if s in seen or not s:
				continue
			seen.add(s)
			w = max(w, _tw(s, ln["bold"], 1.0))
		unit.append(w)

	want = design["qr"]["size"] * MM
	if not has_text:
		q = min(aw, ah, want or 1e9)
	elif horizontal:
		q = min(ah, aw * 0.5) if not want else min(want, ah, aw * 0.8 - gap)
	else:
		q = min(aw, ah * 0.6) if not want else min(want, aw, ah * 0.8 - gap)
	q = max(q, 8 * MM if min(aw, ah) > 8 * MM else min(aw, ah))
	if want and want > q + 0.01:
		warnings.append(_("QR size reduced to fit the label."))

	if horizontal:
		tw, th = aw - q - gap, ah
	else:
		tw, th = aw, ah - q - gap

	sizes = []
	if has_text:
		lg = lay["line_gap"]
		n = len(lines)
		auto_idx = [i for i, ln in enumerate(lines) if ln["size"] <= 0]
		fixed_cap = sum(CAP * ln["size"] for ln in lines if ln["size"] > 0)
		denom_h = CAP * sum(lines[i]["scale"] for i in auto_idx) + (n - 1) * lg
		s = design["base_pt"] or design["max_pt"]
		s_want = s
		for i in auto_idx:
			if unit[i] > 0:
				s = min(s, tw / (unit[i] * lines[i]["scale"]))
		if auto_idx:
			s = min(s, max(th - fixed_cap, 1) / denom_h if denom_h else s)
		s = max(s, MIN_PT)
		if design["base_pt"] and s < design["base_pt"] - 0.05:
			warnings.append(
				_(
					"Text size {0}pt does not fit this label; it was reduced to {1}pt. Make the label bigger or the QR smaller."
				).format(round(design["base_pt"], 1), round(s, 1))
			)
		if s <= MIN_PT + 0.01:
			warnings.append(
				_("Text is at the {0}pt minimum and may overflow; make the label larger.").format(int(MIN_PT))
			)
		for i, ln in enumerate(lines):
			sz = s * ln["scale"] if ln["size"] <= 0 else ln["size"]
			if unit[i] > 0 and unit[i] * sz > tw + 0.01:
				if ln["size"] > 0:
					warnings.append(
						_("Line {0} was reduced from {1}pt to {2}pt to fit the label width.").format(
							i + 1, round(ln["size"], 1), round(tw / unit[i], 1)
						)
					)
				sz = tw / unit[i]
			sizes.append(sz)
		smallest = min((sz for sz, ln in zip(sizes, lines, strict=False) if ln["text"].strip()), default=99)
		if smallest < 6:
			warnings.append(
				_("Smallest text is {0}pt - hard to read when printed.").format(round(smallest, 1))
			)
		caps = [CAP * sz for sz in sizes]
		gap_l = lg * (s if auto_idx else max(sizes))
		bh = sum(caps) + (len(lines) - 1) * gap_l
		bw = max((u * sz for u, sz in zip(unit, sizes, strict=False)), default=0)
	else:
		caps, gap_l, bh, bw = [], 0, 0, 0

	def place(start, avail, size):
		return start + {"start": 0, "center": (avail - size) / 2, "end": avail - size}[lay["group"]]

	if has_text:
		if horizontal:
			gw = q + gap + bw
			gx = place(ax, aw, gw)
			qr_x = gx if pos == "right" else gx + bw + gap
			tx = gx + q + gap if pos == "right" else gx
			cross = {"start": 0, "center": 0.5, "end": 1}[lay["align"]]
			qr_top = ay + (ah - q) * cross
			t_top = ay + (ah - bh) * cross
		else:
			gh = q + gap + bh
			gy = place(ay, ah, gh)
			qr_top = gy if pos == "bottom" else gy + bh + gap
			t_top = gy + q + gap if pos == "bottom" else gy
			cross = {"start": 0, "center": 0.5, "end": 1}[lay["align"]]
			qr_x = ax + (aw - q) * cross
			tx = ax + (aw - bw) * cross
	else:
		qr_x, qr_top = ax + (aw - q) / 2, ay + (ah - q) / 2
		tx, t_top = ax, ay

	geo = {
		"W": W,
		"H": H,
		"q": q,
		"qr_x": qr_x,
		"qr_top": qr_top,
		"tx": tx,
		"t_top": t_top,
		"bw": bw,
		"bh": bh,
		"caps": caps,
		"sizes": sizes,
		"gap_l": gap_l,
		"unit": unit,
	}

	# scan-ability: smallest module across the batch's densest QR
	dense = max(ctxs, key=lambda c: len(lp_types.payload(label_type, c["code"])))
	_r, n_mod = qr_runs(lp_types.payload(label_type, dense["code"]), design["qr"]["ec"])
	mod_mm = q / MM / n_mod
	geo["module_mm"] = mod_mm
	geo["modules"] = n_mod
	if mod_mm < 0.25:
		warnings.append(
			_(
				"QR modules are {0}mm - too small for most label printers. Enlarge the QR or shorten the code."
			).format(round(mod_mm, 2))
		)
	elif mod_mm < 0.33:
		warnings.append(
			_("QR modules are {0}mm - borderline for 203dpi thermal printers.").format(round(mod_mm, 2))
		)
	quiet = min(qr_x, W - qr_x - q, qr_top, H - qr_top - q)
	if quiet < 2 * mod_mm * MM:
		warnings.append(_("Little white space around the QR; scanners need a clear border."))
	geo["warnings"] = warnings
	return geo


# ---------------------------------------------------------------- PDF


def _esc(s):
	b = s.encode("cp1252", "replace")
	return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def label_ops(design, geo, ctx, label_type):
	"""Content-stream operators for one label, origin at its bottom-left."""
	H = geo["H"]
	q = geo["q"]
	runs, n = qr_runs(lp_types.payload(label_type, ctx["code"]), design["qr"]["ec"])
	mod = q / n
	ops = [b"0 g"]
	x0, y0 = geo["qr_x"], H - geo["qr_top"] - q
	rects = [
		"%.3f %.3f %.3f %.3f re" % (x0 + x * mod, y0 + (n - 1 - y) * mod, w * mod + 0.02, mod + 0.02)
		for x, y, w in runs
	]
	ops.append(" ".join(rects).encode())
	ops.append(b"f")
	top = geo["t_top"]
	lay = design["layout"]
	for ln, cap, sz in zip(design["lines"], geo["caps"], geo["sizes"], strict=False):
		s = fill(ln["text"], ctx, ln["upper"])
		if s:
			w = _tw(s, ln["bold"], sz)
			x = (
				geo["tx"]
				+ {"left": 0, "center": (geo["bw"] - w) / 2, "right": geo["bw"] - w}[lay["text_align"]]
			)
			ops.append(
				b"BT /%s %.2f Tf %.3f %.3f Td (" % (FONT[ln["bold"]][0].encode(), sz, x, H - (top + cap))
				+ _esc(s)
				+ b") Tj ET"
			)
		top += cap + geo["gap_l"]
	return b"\n".join(ops)


class Pdf:
	"""Minimal multi-page PDF with the two standard Helvetica fonts."""

	def __init__(self):
		self.pages = []  # (w, h, content)

	def add(self, w, h, content):
		self.pages.append((w, h, zlib.compress(content, 1)))

	def tobytes(self):
		out = io.BytesIO()
		offs = []

		def obj(body):
			offs.append(out.tell())
			out.write(b"%d 0 obj\n" % len(offs))
			out.write(body)
			out.write(b"\nendobj\n")

		out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
		n = len(self.pages)
		kids = b" ".join(b"%d 0 R" % (5 + 2 * i) for i in range(n))
		obj(b"<< /Type /Catalog /Pages 2 0 R >>")
		obj(b"<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids))
		obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
		obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
		for i, (w, h, c) in enumerate(self.pages):
			obj(
				b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] /Contents %d 0 R "
				b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % (w, h, 6 + 2 * i)
			)
			obj(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(c) + c + b"\nendstream")
		xref = out.tell()
		out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(offs) + 1))
		for o in offs:
			out.write(b"%010d 00000 n \n" % o)
		out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(offs) + 1, xref))
		return out.getvalue()


# ---------------------------------------------------------------- layout modes


def sheet_grid(design, layout):
	"""(paper_w, paper_h, cols, rows, x0, y0, dx, dy) in points for sheet mode."""
	sh = layout.get("sheet") or {}
	pw, ph = PAPER.get(sh.get("paper"), PAPER["A4"])
	if sh.get("orientation") == "landscape":
		pw, ph = ph, pw
	pw, ph = pw * MM, ph * MM
	sm = _num(sh.get("margin"), 0, 40, 8) * MM
	g = _num(sh.get("gap"), 0, 20, 2) * MM
	lw, lh = design["page"]["w"] * MM, design["page"]["h"] * MM
	cols = max(1, int((pw - 2 * sm + g) // (lw + g)))
	rows = max(1, int((ph - 2 * sm + g) // (lh + g)))
	if lw > pw - 2 * sm or lh > ph - 2 * sm:
		frappe.throw(_("The label is larger than the sheet. Use one label per page or a bigger sheet."))
	x0 = (pw - (cols * lw + (cols - 1) * g)) / 2
	y0 = ph - (ph - (rows * lh + (rows - 1) * g)) / 2
	return pw, ph, cols, rows, x0, y0, lw + g, lh + g


def labels_per_page(design, layout):
	if (layout or {}).get("mode") != "sheet":
		return 1
	_, _, c, r, *_rest = sheet_grid(design, layout)
	return c * r


def render_pdf(design, layout, ctxs, label_type, geo=None):
	"""A PDF of `ctxs` (list of dicts with `code`)."""
	geo = geo or solve(design, ctxs, label_type)
	pdf = Pdf()
	if (layout or {}).get("mode") != "sheet":
		for ctx in ctxs:
			pdf.add(geo["W"], geo["H"], label_ops(design, geo, ctx, label_type))
		return pdf.tobytes()
	pw, ph, cols, rows, x0, y0, dx, dy = sheet_grid(design, layout)
	per = cols * rows
	guides = bool((layout.get("sheet") or {}).get("guides"))
	for start in range(0, len(ctxs), per):
		parts = []
		for k, ctx in enumerate(ctxs[start : start + per]):
			r, c = divmod(k, cols)
			tx, ty = x0 + c * dx, y0 - r * dy - geo["H"]
			parts.append(b"q 1 0 0 1 %.3f %.3f cm" % (tx, ty))
			parts.append(label_ops(design, geo, ctx, label_type))
			if guides:
				parts.append(b"0.5 G 0.25 w %.3f %.3f %.3f %.3f re S 0 G" % (0, 0, geo["W"], geo["H"]))
			parts.append(b"Q")
		pdf.add(pw, ph, b"\n".join(parts))
	return pdf.tobytes()


def preview_png(design, layout, ctxs, label_type, dpi=220):
	"""(png data URI, geometry) of the first page, drawn by the same code the printer output uses."""
	geo = solve(design, ctxs or [dict(lp_types.TYPES[label_type]["sample"])], label_type)
	pages = labels_per_page(design, layout)
	pdfb = render_pdf(design, layout, ctxs[: max(1, pages)], label_type, geo)
	doc = pymupdf.open(stream=pdfb, filetype="pdf")
	pix = doc[0].get_pixmap(dpi=dpi)
	png = base64.b64encode(pix.tobytes("png")).decode()
	doc.close()
	return "data:image/png;base64," + png, geo
