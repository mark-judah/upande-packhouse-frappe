import frappe
from upande_packhouse.spec_autofill import build_spec_rows

SRC_WH = "Kapkolia Receiving Cold Store - KR"

HEADER = dict(
    customer="FLAMINGO  UK",
    company="Karen Roses",
    business_unit="Roses",
    farm="Kapkolia",
    custom_farm="Kapkolia",
    selling_price_list="GBP Price List",
    currency="GBP",
    custom_delivery_point="WILHAR",
    custom_shipping_agent="WILMAR FLOWERS LTD",
    custom_consignee="FLAMINGO FLOWERS LTD",
    transaction_date=frappe.utils.today(),
    delivery_date=frappe.utils.add_days(frappe.utils.today(), 4),
    custom_truck_details="TEST TRUCK",
)


def _new_so(order_name, s_number):
    so = frappe.new_doc("Sales Order")
    so.update(HEADER)
    so.custom_order_name = order_name
    so.custom_s_number = s_number
    return so


def _add_rows(so, spec, selections, next_mix_group=1, next_bunch_group=1):
    result = build_spec_rows(spec, selections, next_mix_group, next_bunch_group, source_warehouse=SRC_WH)
    for row in result["rows"]:
        so.append("items", row)


def run():
    created = {}

    # ---------------- ORDER 1: Straight box, 5 varieties ----------------
    so1 = _new_so("KPK2-STRAIGHT-5VAR", "PKT-K2-1")
    _add_rows(so1, "SAWTROSWHT004 WHITE  52CM",
              [{"line_idx": 0, "box_idx": 0, "variety": "Athena", "boxes": 2}])
    _add_rows(so1, "SMIRNOVA PF-NELLI CLASSIC SPRAY 72CM", [
        {"line_idx": 0, "box_idx": 0, "variety": "Odilia", "boxes": 2},
        {"line_idx": 1, "box_idx": 0, "variety": "Alicia", "boxes": 2},
        {"line_idx": 3, "box_idx": 0, "variety": "Mirabel", "boxes": 2},
        {"line_idx": 4, "box_idx": 0, "variety": "Marisa", "boxes": 2},
    ])
    so1.insert(ignore_permissions=True)
    so1.submit()
    created["order1_straight_5var"] = so1.name

    # ---------------- ORDER 2: Mixed box, 3 groups ----------------
    so2 = _new_so("KPK2-MIXEDBOX-3GRP", "PKT-K2-2")
    _add_rows(so2, "OOO GOLDEN MIX SPRAY 52CM", [
        {"line_idx": 0, "box_idx": 0, "variety": "Odilia", "boxes": 2},
        {"line_idx": 1, "box_idx": 0, "variety": "Alicia", "boxes": 2},
    ], next_mix_group=1)
    _add_rows(so2, "OOO GOLDEN MIX SPRAY 52CM", [
        {"line_idx": 3, "box_idx": 0, "variety": "Mirabel", "boxes": 2},
        {"line_idx": 4, "box_idx": 0, "variety": "Marisa", "boxes": 2},
    ], next_mix_group=2)
    _add_rows(so2, "OOO GOLDEN MIX SPRAY 52CM", [
        {"line_idx": 0, "box_idx": 0, "variety": "Dinara", "boxes": 2},
        {"line_idx": 1, "box_idx": 0, "variety": "Alicia", "boxes": 2},
        {"line_idx": 3, "box_idx": 0, "variety": "Mirabel", "boxes": 2},
    ], next_mix_group=3)
    so2.insert(ignore_permissions=True)
    so2.submit()
    created["order2_mixedbox_3grp"] = so2.name

    # ---------------- ORDER 3: Mixed bunch, 3 groups ----------------
    so3 = _new_so("KPK2-MIXEDBUNCH-3GRP", "PKT-K2-3")
    _add_rows(so3, "SYSU-02490-20 52CM",
              [{"line_idx": 1, "box_idx": 0, "variety": "Odilia", "boxes": 3}],
              next_bunch_group=1)
    _add_rows(so3, "CH STOCK MIX 42CM MADAM RED 42CM", [
        {"line_idx": 2, "box_idx": 0, "variety": "Athena", "boxes": 2},
        {"line_idx": 5, "box_idx": 0, "variety": "Fuchsiana", "boxes": 2},
    ], next_bunch_group=2)
    _add_rows(so3, "WR03 COOL MIX 42CM", [
        {"line_idx": 0, "box_idx": 2, "variety": "Athena", "boxes": 2},
        {"line_idx": 2, "box_idx": 2, "variety": "Nightingale", "boxes": 2},
    ], next_bunch_group=3)
    so3.insert(ignore_permissions=True)
    so3.submit()
    created["order3_mixedbunch_3grp"] = so3.name

    # ---------------- ORDER 4: Complex (straight + mixed box + mixed bunch) ----------------
    so4 = _new_so("KPK2-COMPLEX", "PKT-K2-4")
    _add_rows(so4, "SAWTROSWHT004 WHITE  52CM",
              [{"line_idx": 0, "box_idx": 0, "variety": "Athena", "boxes": 1}])
    _add_rows(so4, "SMIRNOVA PF-NELLI CLASSIC SPRAY 72CM",
              [{"line_idx": 0, "box_idx": 0, "variety": "Odilia", "boxes": 1}])
    _add_rows(so4, "OOO GOLDEN MIX SPRAY 52CM", [
        {"line_idx": 1, "box_idx": 0, "variety": "Alicia", "boxes": 2},
        {"line_idx": 4, "box_idx": 0, "variety": "Marisa", "boxes": 2},
    ], next_mix_group=1)
    _add_rows(so4, "CH STOCK MIX 42CM MADAM RED 42CM", [
        {"line_idx": 2, "box_idx": 0, "variety": "Athena", "boxes": 1},
        {"line_idx": 5, "box_idx": 0, "variety": "Fuchsiana", "boxes": 1},
    ], next_bunch_group=1)
    so4.insert(ignore_permissions=True)
    so4.submit()
    created["order4_complex"] = so4.name

    frappe.db.commit()
    for k, v in created.items():
        print(k, "->", v)
        so = frappe.get_doc("Sales Order", v)
        for it in so.items:
            print("   ", it.name, it.item_code, "qty(uom units)=", it.qty, "uom=", it.uom,
                  "stock_qty=", it.stock_qty, "cut_stage=", it.custom_cut_stage,
                  "length=", it.custom_length, "mix_group=", it.custom_mix_group,
                  "bunch_group=", it.custom_bunch_group)
