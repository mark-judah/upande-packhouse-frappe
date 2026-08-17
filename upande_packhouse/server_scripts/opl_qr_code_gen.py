import os
import time
import frappe
import qrcode

def generate_qr_code(opl_url, opl_name):
    unique_id = int(time.time())

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )

    data_str = str(opl_url)

    qr.add_data(data_str)
    qr.make(fit=True)

    img = qr.make_image(fill="black", back_color="white")

    qr_codes_dir_path = frappe.utils.get_files_path("qr_codes")
    os.makedirs(qr_codes_dir_path, exist_ok = True)

    file_path = os.path.join(qr_codes_dir_path, f"{opl_name}_{unique_id}.png")
    img.save(file_path)

    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_url": f"/files/qr_codes/{opl_name}_{unique_id}.png",
        "attached_to_doctype": "Order Pick List",
        "attached_to_name": opl_name,
        "is_private": 0
    })

    file_doc.insert()

    frappe.db.set_value("Order Pick List", opl_name, "custom_qr_code", file_doc.file_url)

    final_qr_code_url = file_doc.file_url

    return final_qr_code_url


