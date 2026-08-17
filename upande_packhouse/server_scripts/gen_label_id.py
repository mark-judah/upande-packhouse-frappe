import base64
import json
import os
import time
from io import BytesIO
import json
import base64
import qrcode
import frappe
import fitz  # PyMuPDF
from frappe.utils.pdf import get_pdf
from frappe.utils import get_files_path, now

@frappe.whitelist()
def generate_id(
	label_doc_name,
	action,
	variety=None,
	farm=None,
	stem_length=None,
	bunch_size=None,
	grader=None,
	day_code=None,
	farm_code=None,
	no_of_labels=0,
	row_id=None,
	from_position=0,
	to_position=0,
):
	# Generate Bucket id
	# Encode the bucket id and variety in the qr code
	def get_next_sequence(action, increment_by=1):
		sequence_doc = frappe.get_single("QR Sequence")
		if action == "Harvesting Label":
			counter = sequence_doc.bucket_counter or 0
			sequence_doc.bucket_counter = counter + increment_by
		elif action == "Bunch Label":
			counter = sequence_doc.bunch_counter or 0
			sequence_doc.bunch_counter = counter + increment_by
		elif action == "Grader Label":
			counter = sequence_doc.grader_counter or 0
			sequence_doc.grader_counter = counter + increment_by
		sequence_doc.save()
		frappe.db.commit()
		return counter

	unique_id = int(time.time())

	qr_codes_dir_path = frappe.utils.get_files_path("qr_codes")
	os.makedirs(qr_codes_dir_path, exist_ok=True)

	no_of_labels_int = int(no_of_labels)

	# Harvesting label
	if action == "Harvesting Label":
		base_number = get_next_sequence(action, increment_by=no_of_labels_int)

		for i in range(1, no_of_labels_int + 1):
			bucket_number = base_number + i
			bucket_id = f"BUCKET-{bucket_number}"

			qr_data = {bucket_id: "bucket"}

			qr_data_string = json.dumps(qr_data)

			qr = qrcode.QRCode(
				version=1,
				error_correction=qrcode.constants.ERROR_CORRECT_L,
				box_size=4,
				border=2,
			)

			qr.add_data(qr_data_string)
			qr.make(fit=True)
			qr_img = qr.make_image(fill="black", back_color="white")

			file_name = f"{label_doc_name}_{unique_id}_{i}.png"
			file_path = os.path.join(qr_codes_dir_path, file_name)
			qr_img.save(file_path)

			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": f"/files/qr_codes/{file_name}",
					"attached_to_doctype": "Stock Entry",
					"attached_to_name": label_doc_name,
					"is_private": 0,
				}
			)

			file_doc.insert(ignore_permissions=True)

			qr_doc = frappe.get_doc(
				{
					"doctype": "Bucket QR Code",
					"id": bucket_id,
					"item_code": variety,
					"qr_code_image": file_doc.file_url,
					"label_print_doc": label_doc_name,
				}
			)
			qr_doc.insert(ignore_permissions=True)
			frappe.db.commit()

	if action == "Bunch Label":
		base_number = get_next_sequence(action, increment_by=no_of_labels_int)

		for i in range(1, no_of_labels_int + 1):
			bunch_number = base_number + i
			bunch_id = f"BUNCH-{bunch_number}"

			qr_data = {
					# "farm": farm,
					# "variety": variety,
					# "stem_length": stem_length,
					# "bunch_size": bunch_size,
				"bunch_id": bunch_id,
			}

			qr_data_string = json.dumps(qr_data)

			qr = qrcode.QRCode(
				version=1,
				error_correction=qrcode.constants.ERROR_CORRECT_L,
				box_size=4,
				border=2,
			)

			qr.add_data(qr_data_string)
			qr.make(fit=True)
			qr_img = qr.make_image(fill="black", back_color="white")

			file_name = f"{label_doc_name}_{unique_id}_{i}.png"
			file_path = os.path.join(qr_codes_dir_path, file_name)
			qr_img.save(file_path)

			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": f"/files/qr_codes/{file_name}",
					"attached_to_doctype": "Stock Entry",
					"attached_to_name": label_doc_name,
					"is_private": 0,
				}
			)

			file_doc.insert(ignore_permissions=True)

			qr_doc = frappe.get_doc(
				{
					"doctype": "Bunch QR Code",
					"id": bunch_id,
					"item_code": variety,
					"qr_code_image": file_doc.file_url,
					"label_print_doc": label_doc_name,
					"bunch_size": bunch_size,
					"stem_length": stem_length,
					"farm": farm,
					"farm_code": farm_code,
				}
			)
			qr_doc.insert(ignore_permissions=True)
			frappe.db.commit()

	if action == "Grader Label":
		for i in range(1, no_of_labels_int + 1):
			bunch_id = f"GRADER-{frappe.generate_hash(length=10)}-{i}"

			qr_data = {
				"grader": grader,
			}

			qr_data_string = json.dumps(qr_data)

			qr = qrcode.QRCode(
				version=1,
				error_correction=qrcode.constants.ERROR_CORRECT_L,
				box_size=4,
				border=2,
			)

			qr.add_data(qr_data_string)
			qr.make(fit=True)
			qr_img = qr.make_image(fill="black", back_color="white")

			file_name = f"{label_doc_name}_{unique_id}_{i}.png"
			file_path = os.path.join(qr_codes_dir_path, file_name)
			qr_img.save(file_path)

			file_doc = frappe.get_doc(
				{
					"doctype": "File",
					"file_url": f"/files/qr_codes/{file_name}",
					"attached_to_doctype": "Stock Entry",
					"attached_to_name": label_doc_name,
					"is_private": 0,
				}
			)

			file_doc.insert(ignore_permissions=True)

			qr_doc = frappe.get_doc(
				{
					"doctype": "Grader QR Code",
					"qr_code_image": file_doc.file_url,
					"label_print_doc": label_doc_name,
					"grader": grader,
					"day_code": day_code,
				}
			)
			qr_doc.insert(ignore_permissions=True)
			frappe.db.commit()

	if action == "Shelf Label":
		# Check the from and to positions and find the number of labels needed
		positions = range(int(from_position), int(to_position) + 1)
		levels = ["T", "M", "B"]

		for pos in positions:
			for level in levels:
				shelf_id = f"{row_id}{pos}{level}"

				qr_data = {"shelf": shelf_id}

				qr_data_string = json.dumps(qr_data)

				qr = qrcode.QRCode(
					version=1,
					error_correction=qrcode.constants.ERROR_CORRECT_L,
					box_size=4,
					border=2,
				)

				qr.add_data(qr_data_string)
				qr.make(fit=True)
				qr_img = qr.make_image(fill="black", back_color="white")

				file_name = f"{label_doc_name}_{unique_id}_{shelf_id}.png"
				file_path = os.path.join(qr_codes_dir_path, file_name)
				qr_img.save(file_path)

				file_doc = frappe.get_doc(
					{
						"doctype": "File",
						"file_url": f"/files/qr_codes/{file_name}",
						"attached_to_doctype": "Stock Entry",
						"attached_to_name": label_doc_name,
						"is_private": 0,
					}
				)
				file_doc.insert(ignore_permissions=True)

				if not frappe.db.exists("Shelf QR Code", shelf_id):
					qr_doc = frappe.get_doc(
						{
							"doctype": "Shelf QR Code",
							"shelf_id": shelf_id,
							"qr_code_image": file_doc.file_url,
							"label_print_doc": label_doc_name,
							"row_id": row_id,
							"position": pos,
						}
					)
					qr_doc.insert(ignore_permissions=True)
					frappe.db.commit()
				else:
					frappe.msgprint(f"Shelf {shelf_id} already exists, skipped.")
	frappe.response["message"] = "Label created Successfully"


# ============================================================
# ENTRY POINT - Called from UI
# ============================================================
@frappe.whitelist()
def generate_batch_table_labels(docname):
    """Triggers the long-running background process for the child table"""
    frappe.enqueue(
        'upande_packhouse.server_scripts.gen_label_id.run_label_generation_job',
        docname=docname,
        queue='long',
        timeout=3600
    )
    return "Background job started. You will receive a notification when the labels are ready."


# ============================================================
# BACKGROUND JOB - Runs in queue
# ============================================================
def run_label_generation_job(docname):
    """Enhanced background job with optimized PDF generation"""
    frappe.log_error(f"Starting job for {docname}", "Label Debug: Step 1")

    job_owner = None
    try:
        parent_doc = frappe.get_doc("Label Print", docname)
        job_owner = parent_doc.owner
        
        # 1. Filter and Calculate
        valid_rows = [row for row in parent_doc.details if int(row.no_of_labels) > 0]
        total_count = sum(int(row.no_of_labels) for row in valid_rows)
        
        if total_count == 0:
            return

        # 2. Update Sequence
        seq_doc = frappe.get_single("QR Sequence")
        start_num = (seq_doc.bunch_counter or 0) + 1
        seq_doc.bunch_counter = (seq_doc.bunch_counter or 0) + total_count
        seq_doc.save(ignore_permissions=True)
        
        # 3. Fetch farm code ONCE from parent document's farm_name
        farm_code = parent_doc.farm_name
        if parent_doc.farm_name:
            try:
                farm_code = frappe.get_value("Farm", parent_doc.farm_name, "kephis_farm_id") or parent_doc.farm_name
            except Exception as e:
                frappe.log_error(f"Could not fetch farm code for {parent_doc.farm_name}: {str(e)}", "Farm Code Fetch")
                farm_code = parent_doc.farm_name
        
        qr_docs = []
        label_data_for_pdf = []
        current_idx = start_num

        # 4. Process Rows - collect data for both DB and PDF
        for row in valid_rows:
            qty = int(row.no_of_labels)
            
            for i in range(qty):
                bunch_id = f"BUNCH-{current_idx}"
                
                # Prepare label data for PDF
                label_info = {
                    "bunch_id": bunch_id,
                    "variety": row.variety,
                    "bunch_size": row.bunch_size,
                    "stem_length": row.stem_length,
                    "farm": parent_doc.farm_name,
                    "farm_code": farm_code
                }
                label_data_for_pdf.append(label_info)

                # Prepare data for database insert (NO qr_code_image file)
                qr_docs.append({
                    "name": bunch_id,
                    "id": bunch_id,
                    "item_code": row.variety,
                    "label_print_doc": docname,
                    "bunch_size": row.bunch_size,
                    "stem_length": row.stem_length,
                    "farm": parent_doc.farm_name,
                    "farm_code": farm_code,
                    "owner": job_owner,
                    "creation": now(),
                    "modified": now()
                })
                current_idx += 1

        # 5. Database Insert
        if qr_docs:
            fields = list(qr_docs[0].keys())
            values = [list(d.values()) for d in qr_docs]
            
            try:
                frappe.db.bulk_insert("Bunch QR Code", fields=fields, values=values)
                frappe.db.commit()
            except Exception as db_err:
                frappe.db.rollback()
                raise db_err

        # 6. Generate PDF with all labels using PyMuPDF (in-memory, no files saved)
        pdf_base64 = None
        pdf_file_url = None
        
        if label_data_for_pdf:
            pdf_base64 = generate_batch_labels_pdf_pymupdf(label_data_for_pdf, docname)
            
            # Save PDF as a file attachment to the document
            if pdf_base64:
                pdf_file_url = save_pdf_as_attachment(
                    pdf_base64, 
                    docname, 
                    f"batch_labels_{docname}.pdf",
                    "Label Print"
                )

        # 7. CREATE SYSTEM NOTIFICATION with PDF link
        notification_doc = frappe.new_doc("Notification Log")
        notification_doc.for_user = job_owner
        notification_doc.subject = f"Batch Complete: {total_count} labels generated"
        
        if pdf_file_url:
            # Create clickable link to PDF in notification
            notification_doc.email_content = f"""The label generation for {docname} has finished. 
            <br><br>
            <strong>{len(label_data_for_pdf)} labels</strong> have been generated.
            <br><br>
            <a href="{pdf_file_url}" target="_blank" style="background-color: #2490ef; color: white; padding: 8px 16px; text-decoration: none; border-radius: 4px; display: inline-block;">Download PDF Labels</a>
            """
        else:
            notification_doc.email_content = f"The label generation for {docname} has finished. You can now view the labels (PDF generation encountered an issue)."
        
        notification_doc.document_type = "Label Print"
        notification_doc.document_name = docname
        notification_doc.insert(ignore_permissions=True)
        
        # Realtime notification with PDF link
        if pdf_file_url:
            frappe.publish_realtime('msgprint', {
                'message': f"Batch complete! {total_count} labels ready. <a href='{pdf_file_url}' target='_blank'>Download PDF</a>",
                'indicator': 'green'
            }, user=job_owner)
        else:
            frappe.publish_realtime('msgprint', {
                'message': f"Batch for {docname} complete! {total_count} labels ready.",
                'indicator': 'green'
            }, user=job_owner)
            
        frappe.log_error(f"Job completed successfully for {docname}", "Label Generation Success")

    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Label Generation Failed")
        
        # Notify user of failure
        notification_doc = frappe.new_doc("Notification Log")
        notification_doc.for_user = job_owner or frappe.session.user
        notification_doc.subject = f"Batch Failed: {docname}"
        notification_doc.email_content = f"An error occurred while generating labels. Please contact admin. Error: {str(e)[:100]}"
        # document_type/name are required by Notification Log's email hook; without
        # them send_notification_email() crashes on slug(None) and masks the real error.
        notification_doc.document_type = "Label Print"
        notification_doc.document_name = docname
        notification_doc.insert(ignore_permissions=True)


# ============================================================
# QR CODE GENERATION
# ============================================================
def generate_qr_code_on_demand(qr_data_dict):
    """Generate a single QR code on-demand in memory (NO file creation)"""
    # Handle different input types
    if isinstance(qr_data_dict, str):
        try:
            if qr_data_dict.strip().startswith('{'):
                qr_data_dict = json.loads(qr_data_dict)
                qr_data_string = json.dumps(qr_data_dict, separators=(',', ':'), ensure_ascii=False)
            else:
                qr_data_string = qr_data_dict
        except Exception as e:
            frappe.log_error(f"QR parsing error: {str(e)}", "QR Generation")
            qr_data_string = qr_data_dict
    else:
        qr_data_string = json.dumps(qr_data_dict, separators=(',', ':'), ensure_ascii=False)
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=4,
        border=2,
    )
    qr.add_data(qr_data_string)
    qr.make(fit=True)
    qr_img = qr.make_image(fill='black', back_color='white')
    
    # Generate in-memory, return base64 data URI
    buffered = BytesIO()
    qr_img.save(buffered, format="PNG")
    img_base64 = base64.b64encode(buffered.getvalue()).decode()
    
    return f"data:image/png;base64,{img_base64}"


# ============================================================
# PDF GENERATION - PyMuPDF
# ============================================================
def generate_batch_labels_pdf_pymupdf(label_data_list, parent_doc_name):
    """
    Generate PDF for batch table labels using PyMuPDF (fitz) - FAST!
    NO file creation - all QR codes generated in-memory
    
    Args:
        label_data_list: List of dicts with label information
        parent_doc_name: The Label Print document name
    
    Returns:
        Base64 encoded PDF string
    """
    try:
        # A4 landscape: 297mm x 210mm = 841.89 x 595.28 points (1mm = 2.834645669 points)
        PAGE_WIDTH = 841.89
        PAGE_HEIGHT = 595.28
        
        # Create PDF document
        pdf_doc = fitz.open()
        
        for label_index, label_data in enumerate(label_data_list):
            # Create new page for each label (A4 landscape)
            page = pdf_doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            
            # Extract label information
            bunch_id = label_data.get('bunch_id', '')
            variety = label_data.get('variety', '')
            bunch_size = label_data.get('bunch_size', '')
            stem_length = label_data.get('stem_length', '')
            farm = label_data.get('farm', '')
            farm_code = label_data.get('farm_code', farm)
            
            # Prepare QR data - ONLY bunch_id for fast scanning
            qr_data = {
                "bunch_id": bunch_id
            }
            
            # Generate QR code IN-MEMORY
            qr_base64 = generate_qr_code_on_demand(qr_data)
            # Remove data URI prefix to get pure base64
            qr_base64_clean = qr_base64.split(',')[1] if ',' in qr_base64 else qr_base64
            qr_image_bytes = base64.b64decode(qr_base64_clean)
            
            # Label dimensions (160mm x 40mm at top-left)
            # 160mm = 453.54 points, 40mm = 113.39 points
            label_width = 453.54
            label_height = 113.39
            label_x = 0
            label_y = 0
            
            # QR code area (40mm x 40mm)
            qr_size = 113.39
            qr_rect = fitz.Rect(label_x, label_y, label_x + qr_size, label_y + qr_size)
            
            # Insert QR code image
            page.insert_image(qr_rect, stream=qr_image_bytes)
            
            # Text area starts after QR code
            text_x = label_x + qr_size + 5  # 5 points padding
            text_y = label_y + 10
            
            # Font size: 6mm = 17 points
            font_size = 17
            line_height = 20
            
            # Add text content - all in bold
            page.insert_text(
                (text_x, text_y + line_height),
                variety or '',
                fontsize=font_size,
                fontname="hebo",  # Helvetica Bold
                color=(0, 0, 0)
            )
            
            page.insert_text(
                (text_x, text_y + line_height * 2),
                bunch_size or '',
                fontsize=font_size,
                fontname="hebo",  # Helvetica Bold
                color=(0, 0, 0)
            )
            
            page.insert_text(
                (text_x, text_y + line_height * 3),
                stem_length or '',
                fontsize=font_size,
                fontname="hebo",  # Helvetica Bold
                color=(0, 0, 0)
            )
            
            page.insert_text(
                (text_x, text_y + line_height * 4),
                farm_code or '',
                fontsize=font_size,
                fontname="hebo",  # Helvetica Bold
                color=(0, 0, 0)
            )
        
        # Save PDF to bytes
        pdf_bytes = pdf_doc.tobytes()
        pdf_doc.close()
        
        # Encode to base64
        pdf_base64 = base64.b64encode(pdf_bytes).decode()
        
        frappe.log_error(f"PyMuPDF: Generated PDF with {len(label_data_list)} labels", "PDF Generation Success")
        
        return pdf_base64
        
    except ImportError as ie:
        frappe.log_error(
            f"PyMuPDF not installed. Install with: bench pip install PyMuPDF\nError: {str(ie)}", 
            "PyMuPDF Missing"
        )
        frappe.throw("PyMuPDF is required but not installed. Please contact administrator.")
        
    except Exception as e:
        frappe.log_error(
            f"PyMuPDF PDF generation error: {str(e)}\n{frappe.get_traceback()}", 
            "PyMuPDF PDF Error"
        )
        frappe.throw(f"PDF generation failed: {str(e)}")


# ============================================================
# FILE ATTACHMENT
# ============================================================
def save_pdf_as_attachment(pdf_base64, docname, filename, doctype):
    """
    Save PDF as a file attachment to a document
    
    Args:
        pdf_base64: Base64 encoded PDF content
        docname: Document name to attach to
        filename: PDF filename
        doctype: Document type
        
    Returns:
        File URL for the attached PDF
    """
    try:
        # Decode base64 to binary
        pdf_content = base64.b64decode(pdf_base64)
        
        # Use Frappe's save_file method
        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": filename,
            "attached_to_doctype": doctype,
            "attached_to_name": docname,
            "is_private": 0,
            "content": pdf_content
        })
        
        file_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(f"PDF saved successfully: {file_doc.file_url}", "PDF Save Success")
        
        return file_doc.file_url
        
    except Exception as e:
        frappe.log_error(f"Error saving PDF attachment: {str(e)}\n{frappe.get_traceback()}", "PDF Attachment Error")
        
        # Try alternative method using save_file
        try:
            from frappe.utils.file_manager import save_file
            
            file_doc = save_file(
                fname=filename,
                content=base64.b64decode(pdf_base64),
                dt=doctype,
                dn=docname,
                is_private=0
            )
            
            frappe.db.commit()
            frappe.log_error(f"PDF saved with alternative method: {file_doc.file_url}", "PDF Save Alternative")
            
            return file_doc.file_url
            
        except Exception as e2:
            frappe.log_error(f"Alternative save also failed: {str(e2)}\n{frappe.get_traceback()}", "PDF Save Failed")
            return None


# ============================================================
# UTILITY FUNCTION - Get PDF on demand
# ============================================================
@frappe.whitelist()
def get_batch_labels_pdf(docname):
    """
    Generate PDF on-demand for existing batch labels
    Can be called from UI after labels are generated
    NO file creation - QR codes generated in-memory
    """
    try:
        # Fetch all labels for this document
        labels = frappe.get_all(
            "Bunch QR Code",
            filters={"label_print_doc": docname},
            fields=["id", "item_code", "bunch_size", "stem_length", "farm", "farm_code"],
            order_by="creation asc"
        )
        
        if not labels:
            frappe.throw(f"No labels found for document {docname}")
        
        # Prepare label data
        label_data_list = []
        for label in labels:
            # Fetch farm code from Farm doctype
            farm_code = label.get("farm")
            if label.get("farm"):
                try:
                    farm_code = frappe.get_value("Farm", label.get("farm"), "kephis_farm_id") or label.get("farm")
                except Exception:
                    farm_code = label.get("farm_code", label.get("farm"))
            
            label_data_list.append({
                "bunch_id": label.get("id"),
                "variety": label.get("item_code"),
                "bunch_size": label.get("bunch_size"),
                "stem_length": label.get("stem_length"),
                "farm": label.get("farm"),
                "farm_code": farm_code
            })
        
        # Generate PDF (PyMuPDF - in-memory, no files)
        pdf_base64 = generate_batch_labels_pdf_pymupdf(label_data_list, docname)
        
        if pdf_base64:
            return {
                "pdf_base64": pdf_base64,
                "pdf_filename": f"batch_labels_{docname}.pdf",
                "count": len(label_data_list)
            }
        else:
            frappe.throw("Failed to generate PDF")
            
    except Exception as e:
        frappe.log_error(f"Error generating batch PDF: {str(e)}", "Batch PDF Error")
        frappe.throw(f"Failed to generate PDF: {str(e)}")