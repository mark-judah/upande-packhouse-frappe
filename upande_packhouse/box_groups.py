"""Which order lines are packed in the same physical box.

A Specification is one box type: its Box Items list the mono bunches and the mixed bunches that go into one
box. The order import turns a spec into order lines -- its mono bunches into mixed-box lines (one mix group)
and each of its mixed bunches into a bunch group ("<spec>::<bunch>") -- and every one of those lines keeps the
spec as its mix name and books the same number of boxes. They are one box: allocated in complete boxes
together, released on one pick list, numbered once.

So a mixed bunch joins the mixed box of its spec with the same number of boxes; the mixed bunches of a spec
with no mono bunches are one box together; a mixed box is its mix group; a straight line is its own box.
"""

from frappe.utils import cint


def spec_of(line):
	"""The spec (box type) a mixed line was filled from: its specification, else its mix name, else its bunch
	group's prefix -- at its length (one spec filled at two lengths is two different boxes, as
	sales_order_engine counts it)."""
	name = str(
		line.get("custom_line") or line.get("specification") or line.get("custom_mix_name") or ""
	).strip()
	if not name and line.get("custom_bunch_group"):
		name = str(line.get("custom_bunch_group")).split("::")[0].strip()
	length = line.get("custom_length") or line.get("required_length") or ""
	return "{0}@{1}".format(name, length) if name else ""


def _name(line):
	return line.get("sales_order_item") or line.get("name")


def box_keys(lines):
	"""{line name: box key} for one order's lines: ("box", mix group) for a mixed box and the mixed bunches of
	its spec, ("bunch", "<spec>|<boxes>") for a spec of mixed bunches only, ("line", name) for a straight
	line. Lines of several orders can be passed together: a spec is matched within its own order."""

	def order(line):
		return line.get("parent") or line.get("sales_order") or ""

	def boxes(line):
		return cint(line.get("custom_number_of_boxes"))

	mono = {}
	for ln in lines:
		if (
			cint(ln.get("custom_mixed_box"))
			and not cint(ln.get("custom_mixed_bunch"))
			and ln.get("custom_mix_group")
		):
			mono.setdefault((order(ln), spec_of(ln), boxes(ln)), str(ln.get("custom_mix_group")))
	out = {}
	for ln in lines:
		if cint(ln.get("custom_mixed_bunch")) and (ln.get("custom_bunch_group") or spec_of(ln)):
			spec = spec_of(ln)
			group = mono.get((order(ln), spec, boxes(ln))) if spec else None
			if group:
				out[_name(ln)] = ("box", group)
			elif spec:
				out[_name(ln)] = ("bunch", "{0}|{1}".format(spec, boxes(ln)))
			else:
				out[_name(ln)] = ("bunch", str(ln.get("custom_bunch_group")))
		elif cint(ln.get("custom_mixed_box")) and ln.get("custom_mix_group"):
			out[_name(ln)] = ("box", str(ln.get("custom_mix_group")))
		else:
			out[_name(ln)] = ("line", _name(ln))
	return out


def mark_boxes(lines):
	"""Set each line's box key on it (`_box`) from the whole list (one order's lines)."""
	keys = box_keys(lines)
	for ln in lines:
		ln["_box"] = keys.get(_name(ln))
	return lines
