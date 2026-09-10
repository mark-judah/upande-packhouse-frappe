import base64
import json
import os
import time
from io import BytesIO
import json
import base64
import qrcode
import frappe
from frappe import _
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
    """Triggers the long-running background process for the child table.

    A Bunch Label Table batch is single-use: resaving the same document
    (deliberately, or a stray double-click / two open tabs) used to
    silently re-run the WHOLE generation again on top of the first --
    a second full set of Bunch QR Code rows, a second consumption of the
    QR/bunch sequence counter, all under one document. labels_generated is
    set the moment generation is triggered (synchronously, before the
    background job even starts, so a near-simultaneous second save can't
    race past this check while the first run is still in flight) and is
    never cleared -- this refusal is permanent for this document by design.
    """
    if frappe.db.get_value("Label Print", docname, "labels_generated"):
        frappe.throw(
            _("Labels have already been generated for {0}. If the PDF never showed up, use the "
              "\"Generate Attachment\" button instead of saving again. For a new batch, create a "
              "new Label Print document.").format(frappe.bold(docname)),
            title=_("Already Generated"),
        )

    frappe.db.set_value("Label Print", docname, "labels_generated", 1, update_modified=False)
    frappe.db.set_value("Label Print", docname, "generation_in_progress", 1, update_modified=False)
    frappe.db.commit()
    _set_progress(docname, 0, _("Generating Labels"), _("Starting..."))

    frappe.enqueue(
        'upande_packhouse.server_scripts.gen_label_id.run_label_generation_job',
        docname=docname,
        queue='long',
        timeout=3600
    )
    return "Background job started. You will receive a notification when the labels are ready."


# ============================================================
# SALVAGE - "Generate Attachment" button
# ============================================================
@frappe.whitelist()
def regenerate_batch_table_attachment(docname):
    """The Bunch QR Code DB insert (step 5 of run_label_generation_job) and
    the PDF-attach step (step 6) are two separate operations -- a worker
    that dies (e.g. OOM-killed on a large batch) between them leaves real,
    already-committed bunches with no attachment ever produced, and no way
    to get one short of re-running the whole batch (which would re-create
    every bunch and hit duplicate-name errors). This re-runs ONLY the PDF
    step, straight from whatever Bunch QR Code rows already exist for this
    document -- safe to call as many times as needed; it always rebuilds
    fresh from current data and replaces any previous attachment rather
    than piling up duplicates.
    """
    doc = frappe.get_doc("Label Print", docname)
    if doc.action != "Bunch Label Table":
        frappe.throw(_("This is only for the Bunch Label Table action."))

    if not frappe.db.exists("Bunch QR Code", {"label_print_doc": docname}):
        frappe.throw(_(
            "No bunches exist yet for {0} -- there's nothing to attach. "
            "Re-run the full generation instead (save the document again)."
        ).format(docname))

    # Bunches confirmed to exist -- this document is "already generated"
    # regardless of how it got that way (e.g. bunches present but the flag
    # never got set, from before this field existed, or a doc edited some
    # other way). Set it here too, not just in generate_batch_table_labels,
    # so a later save can never re-trigger a duplicate full generation.
    if not doc.labels_generated:
        frappe.db.set_value("Label Print", docname, "labels_generated", 1, update_modified=False)

    # Tracked so "Generate Attachment" can warn about a job already in
    # flight -- the client decides whether to warn/confirm; this call
    # itself never refuses to proceed (the whole point is the user can
    # insist and force a fresh regenerate even over a stuck-looking job).
    frappe.db.set_value("Label Print", docname, "generation_in_progress", 1, update_modified=False)
    frappe.db.commit()
    _set_progress(docname, 0, _("Regenerating Attachment"), _("Starting..."))

    frappe.enqueue(
        'upande_packhouse.server_scripts.gen_label_id.salvage_batch_table_attachment_job',
        docname=docname,
        queue='long',
        timeout=3600,
    )
    return "Background job started. You will receive a notification when the attachment is ready."


def _download_links_html(file_urls):
    """One or more download buttons -- a large batch can split into several
    numbered PDF parts (see _labels_per_output_file) to stay under the
    site's max upload size."""
    label = "Download PDF Labels" if len(file_urls) == 1 else None
    return "<br>".join(
        f'<a href="{url}" target="_blank" style="background-color: #2490ef; color: white; '
        f'padding: 8px 16px; text-decoration: none; border-radius: 4px; display: inline-block; '
        f'margin-top: 4px;">{label or f"Download Part {i}"}</a>'
        for i, url in enumerate(file_urls, start=1)
    )


def _download_links_text(file_urls):
    if len(file_urls) == 1:
        return f"<a href='{file_urls[0]}' target='_blank'>Download PDF</a>"
    return " &middot; ".join(f"<a href='{url}' target='_blank'>Part {i}</a>" for i, url in enumerate(file_urls, start=1))


def _set_progress(docname, percent, title, description):
    """Persists progress to the document itself (not just a transient
    realtime broadcast) so it survives the user closing the page and coming
    back -- the whole point of the custom HTML block on the form instead of
    frappe.publish_progress's own popup-style bar (which only exists for as
    long as the page stays open and is never stored anywhere). Also fires a
    plain realtime event so a form left open updates live without polling --
    deliberately NOT frappe.publish_progress's own 'progress' event, since
    that triggers the desk's automatic popup progress dialog, which is
    exactly the UI this replaces."""
    frappe.db.set_value(
        "Label Print", docname,
        {
            "progress_percent": percent,
            "progress_title": title,
            "progress_description": description,
        },
        update_modified=False,
    )
    frappe.db.commit()
    frappe.publish_realtime(
        "label_print_progress",
        {"docname": docname, "percent": percent, "title": title, "description": description},
        doctype="Label Print",
        docname=docname,
    )


def _pdf_progress_reporter(docname, title):
    """A ready-to-pass on_progress(done, total) for attach_batch_labels_pdf /
    build_batch_labels_pdf_file -- see _set_progress for why this doesn't use
    frappe.publish_progress."""
    def _report(done, total):
        percent = (done / total * 100) if total else 100
        _set_progress(docname, percent, title, f"{done:,} / {total:,} labels rendered")
    return _report


def salvage_batch_table_attachment_job(docname):
    """Background half of regenerate_batch_table_attachment -- same
    notify-either-way contract as run_label_generation_job."""
    job_owner = None
    try:
        parent_doc = frappe.get_doc("Label Print", docname)
        job_owner = parent_doc.owner

        bunches = frappe.get_all(
            "Bunch QR Code",
            filters={"label_print_doc": docname},
            fields=["id", "item_code", "bunch_size", "stem_length", "farm", "farm_code"],
            order_by="creation asc",
        )
        if bunches:
            _set_progress(
                docname, 0, _("Regenerating Attachment"),
                _("Found {0:,} existing bunches...").format(len(bunches)),
            )
        label_data_for_pdf = [
            {
                "bunch_id": b.id,
                "variety": b.item_code,
                "bunch_size": b.bunch_size,
                "stem_length": b.stem_length,
                "farm": b.farm,
                "farm_code": b.farm_code or b.farm,
            }
            for b in bunches
        ]

        pdf_file_urls = attach_batch_labels_pdf(
            label_data_for_pdf, docname, "Label Print",
            on_progress=_pdf_progress_reporter(docname, _("Regenerating Attachment")),
        )

        notification_doc = frappe.new_doc("Notification Log")
        notification_doc.for_user = job_owner
        notification_doc.document_type = "Label Print"
        notification_doc.document_name = docname
        if pdf_file_urls:
            part_note = "" if len(pdf_file_urls) == 1 else f" (split into {len(pdf_file_urls)} files)"
            notification_doc.subject = f"Attachment regenerated: {len(label_data_for_pdf)} labels"
            notification_doc.email_content = f"""The attachment for {docname} has been regenerated from its
            {len(label_data_for_pdf)} existing bunches{part_note}.
            <br><br>
            {_download_links_html(pdf_file_urls)}
            """
            frappe.publish_realtime('msgprint', {
                'message': f"Attachment ready! {_download_links_text(pdf_file_urls)}",
                'indicator': 'green',
            }, user=job_owner)
            _set_progress(
                docname, 100, _("Regenerating Attachment"),
                _("Done -- {0:,} labels").format(len(label_data_for_pdf)),
            )
        else:
            notification_doc.subject = f"Attachment generation failed: {docname}"
            notification_doc.email_content = (
                f"Could not regenerate the attachment for {docname}. Check the Error Log "
                "(\"PDF Attachment Error\" / \"PyMuPDF PDF Error\") or contact admin."
            )
            frappe.publish_realtime('msgprint', {
                'message': f"Could not regenerate the attachment for {docname}. See Error Log.",
                'indicator': 'red',
            }, user=job_owner)
            _set_progress(docname, 0, _("Regenerating Attachment"), _("Failed -- see Error Log."))
        notification_doc.insert(ignore_permissions=True)

    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Label Attachment Salvage Failed")
        notification_doc = frappe.new_doc("Notification Log")
        notification_doc.for_user = job_owner or frappe.session.user
        notification_doc.subject = f"Attachment generation failed: {docname}"
        notification_doc.email_content = f"An error occurred while regenerating the attachment for {docname}. Please contact admin."
        notification_doc.document_type = "Label Print"
        notification_doc.document_name = docname
        notification_doc.insert(ignore_permissions=True)
        _set_progress(docname, 0, _("Regenerating Attachment"), _("Failed -- see Error Log."))

    finally:
        frappe.db.set_value("Label Print", docname, "generation_in_progress", 0, update_modified=False)
        frappe.db.commit()


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

        if total_count:
            _set_progress(
                docname, 0, _("Generating Labels"),
                _("Creating {0:,} bunches...").format(total_count),
            )

        if total_count == 0:
            # This used to return silently -- no log, no notification -- which
            # is indistinguishable from a hang/crash to the user. Every other
            # exit path below notifies one way or another; this one must too.
            frappe.log_error(
                f"No labels requested for {docname} -- every row's No of Labels was 0",
                "Label Generation: Nothing To Do",
            )
            notification_doc = frappe.new_doc("Notification Log")
            notification_doc.for_user = job_owner
            notification_doc.subject = f"No labels generated for {docname}"
            notification_doc.email_content = (
                f"The label batch for {docname} finished with nothing to do -- every "
                "row's \"No of Labels\" was 0. Set a quantity on at least one row "
                "(or use the bulk quantity field) and save again to generate labels."
            )
            notification_doc.document_type = "Label Print"
            notification_doc.document_name = docname
            notification_doc.insert(ignore_permissions=True)
            frappe.publish_realtime(
                "msgprint",
                {
                    "message": f"No labels were generated for {docname} -- every row's quantity was 0.",
                    "indicator": "orange",
                },
                user=job_owner,
            )
            _set_progress(docname, 0, _("Generating Labels"), _("Nothing to do -- every row's quantity was 0."))
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

        # 6. Generate the PDF straight to disk (build_batch_labels_pdf_file,
        # bounded-memory chunks -- see its docstring) and attach it. No
        # base64 round-trip here: that would mean holding the whole PDF
        # (~1.33x its binary size) as a second in-memory copy right at the
        # step that used to get this job OOM-killed for large batches.
        pdf_file_urls = []

        if label_data_for_pdf:
            pdf_file_urls = attach_batch_labels_pdf(
                label_data_for_pdf, docname, "Label Print",
                on_progress=_pdf_progress_reporter(docname, _("Generating Labels")),
            )

        # 7. CREATE SYSTEM NOTIFICATION with PDF link(s)
        notification_doc = frappe.new_doc("Notification Log")
        notification_doc.for_user = job_owner
        notification_doc.subject = f"Batch Complete: {total_count} labels generated"

        if pdf_file_urls:
            part_note = "" if len(pdf_file_urls) == 1 else f" across {len(pdf_file_urls)} files"
            notification_doc.email_content = f"""The label generation for {docname} has finished.
            <br><br>
            <strong>{len(label_data_for_pdf)} labels</strong> have been generated{part_note}.
            <br><br>
            {_download_links_html(pdf_file_urls)}
            """
        else:
            notification_doc.email_content = f"The label generation for {docname} has finished. You can now view the labels (PDF generation encountered an issue)."

        notification_doc.document_type = "Label Print"
        notification_doc.document_name = docname
        notification_doc.insert(ignore_permissions=True)

        # Realtime notification with PDF link(s)
        if pdf_file_urls:
            frappe.publish_realtime('msgprint', {
                'message': f"Batch complete! {total_count} labels ready. {_download_links_text(pdf_file_urls)}",
                'indicator': 'green'
            }, user=job_owner)
        else:
            frappe.publish_realtime('msgprint', {
                'message': f"Batch for {docname} complete! {total_count} labels ready.",
                'indicator': 'green'
            }, user=job_owner)

        _set_progress(
            docname, 100, _("Generating Labels"),
            _("Done -- {0:,} labels").format(total_count) if pdf_file_urls
            else _("Bunches created, but the PDF attachment failed -- use \"Generate Attachment\" to retry."),
        )
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
        _set_progress(docname, 0, _("Generating Labels"), _("Failed -- see Error Log."))

    finally:
        # Either way -- success, failure, or the early "nothing to do" return
        # above -- this document is no longer mid-generation, so "Generate
        # Attachment" can stop warning about a job still being in flight.
        frappe.db.set_value("Label Print", docname, "generation_in_progress", 0, update_modified=False)
        frappe.db.commit()


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

# A4 landscape: 297mm x 210mm = 841.89 x 595.28 points (1mm = 2.834645669 points)
_PAGE_WIDTH = 841.89
_PAGE_HEIGHT = 595.28

# Batches of "thousands" of labels used to build ONE fitz.Document holding
# every page (QR image + 4 text lines each) fully in memory before a single
# `.tobytes()`/`.save()` call at the very end. On a memory-limited RQ worker
# that got OOM-killed partway through -- a SIGKILL, not a Python exception,
# so it never reached the `except` block below: no error log, no failure
# notification, nothing. Meanwhile step 5 (the Bunch QR Code DB insert)
# already ran and committed, so the bunches existed with no attachment ever
# generated -- exactly the reported symptom. Building the PDF in bounded
# chunks (each chunk's own small fitz.Document, merged into the master via
# insert_pdf then immediately closed) keeps peak memory roughly constant
# regardless of total label count, so this can't recur no matter how large
# the batch is.
_LABELS_PER_CHUNK = 300


def _add_label_page(pdf_doc, label_data):
    """Renders one label as one A4-landscape page in pdf_doc."""
    page = pdf_doc.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)

    bunch_id = label_data.get('bunch_id', '')
    variety = label_data.get('variety', '')
    bunch_size = label_data.get('bunch_size', '')
    stem_length = label_data.get('stem_length', '')
    farm = label_data.get('farm', '')
    farm_code = label_data.get('farm_code', farm)

    qr_base64 = generate_qr_code_on_demand({"bunch_id": bunch_id})
    qr_base64_clean = qr_base64.split(',')[1] if ',' in qr_base64 else qr_base64
    qr_image_bytes = base64.b64decode(qr_base64_clean)

    # Label dimensions (160mm x 40mm at top-left); QR code area 40mm x 40mm.
    label_x, label_y = 0, 0
    qr_size = 113.39  # 40mm
    qr_rect = fitz.Rect(label_x, label_y, label_x + qr_size, label_y + qr_size)
    page.insert_image(qr_rect, stream=qr_image_bytes)

    text_x = label_x + qr_size + 5  # 5 points padding
    text_y = label_y + 10
    font_size = 17  # 6mm
    line_height = 20

    for i, text in enumerate((variety, bunch_size, stem_length, farm_code), start=1):
        page.insert_text(
            (text_x, text_y + line_height * i),
            text or '',
            fontsize=font_size,
            fontname="hebo",  # Helvetica Bold
            color=(0, 0, 0),
        )


def build_batch_labels_pdf_file(label_data_list, output_path, chunk_size=_LABELS_PER_CHUNK, on_progress=None):
    """Writes label_data_list as one PDF (one page per label) straight to
    output_path, in bounded-size chunks -- see _LABELS_PER_CHUNK's docstring
    for why chunking exists at all. Never holds the whole batch's worth of
    page content in memory at once, and never returns/holds a base64 copy;
    use this (not generate_batch_labels_pdf_pymupdf) for anything that could
    be "thousands" of labels, i.e. every server-side attach path.

    on_progress(done, total), if given, is called after each chunk is
    rendered (not after every single label -- that would be far too chatty
    for a realtime event) so callers can surface progress for what's by far
    the slowest phase of a large batch.

    Each chunk is built as its own small fitz.Document and saved to its own
    temp file -- that part stays cheap regardless of total label count
    (verified: ~1.7s per 300-page chunk, flat). The chunks are then merged
    with pypdf's PdfWriter.append(), NOT fitz's own insert_pdf: profiling
    showed insert_pdf (and in fact just building pages directly into one
    ever-growing fitz.Document, no merging involved at all) gets steadily
    slower per page as the document's existing page count grows -- 5.4ms/page
    at 200 pages, 9.0ms/page at 4000, a MuPDF-internal characteristic, not
    something chunking alone fixes. That's what made 20,000 labels blow past
    10 minutes even after chunking bounded memory. pypdf.PdfWriter.append()
    measured flat at ~0.14s/chunk regardless of how many pages were already
    merged (300 through 2700), so total merge time stays linear in the
    number of chunks instead of quadratic in the total label count.
    """
    import tempfile
    from pypdf import PdfWriter

    total = len(label_data_list)
    done = 0
    chunk_paths = []
    try:
        for start in range(0, total, chunk_size):
            chunk_doc = fitz.open()
            try:
                batch = label_data_list[start:start + chunk_size]
                for label_data in batch:
                    _add_label_page(chunk_doc, label_data)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    chunk_path = tmp.name
                chunk_doc.save(chunk_path)
                chunk_paths.append(chunk_path)
            finally:
                chunk_doc.close()
            done += len(batch)
            if on_progress:
                on_progress(done, total)

        writer = PdfWriter()
        try:
            for chunk_path in chunk_paths:
                writer.append(chunk_path)
            with open(output_path, "wb") as f:
                writer.write(f)
        finally:
            writer.close()
    finally:
        for chunk_path in chunk_paths:
            try:
                os.remove(chunk_path)
            except OSError:
                pass


def generate_batch_labels_pdf_pymupdf(label_data_list, parent_doc_name):
    """
    Generate PDF for batch table labels using PyMuPDF (fitz) - FAST!

    Base64-returning wrapper kept for get_batch_labels_pdf's on-demand
    preview call (small, human-triggered, fine to hold one base64 copy of
    the result). Anything that could run at real batch scale should call
    build_batch_labels_pdf_file directly instead -- see its docstring.

    Args:
        label_data_list: List of dicts with label information
        parent_doc_name: The Label Print document name

    Returns:
        Base64 encoded PDF string
    """
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            build_batch_labels_pdf_file(label_data_list, tmp_path)
            with open(tmp_path, "rb") as f:
                pdf_bytes = f.read()
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

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
# One real label page measured ~3.35KB (20,000 labels -> 63.9MB). Padded up
# for safety margin (longer variety/farm names, less compressible content).
_BYTES_PER_LABEL_ESTIMATE = 4096


def _labels_per_output_file():
    """A single attached PDF must fit under the site's own max upload size
    (frappe.core.api.file.get_max_file_size -- System Settings.max_file_size,
    falling back to 25MB) or the attach step fails outright even though the
    PDF itself built fine: confirmed live -- a real 20,000-label run built a
    63.9MB PDF in 153s (the scaling fix worked), then hit
    MaxFileSizeReachedError on the attach itself, silently (caught, logged,
    returns None) leaving the same "bunches exist, no attachment" state this
    whole feature exists to fix. Bounding pages-per-file the same way pages-
    per-memory-chunk is bounded closes that gap for any batch size.
    """
    from frappe.core.api.file import get_max_file_size

    safe_bytes = int(get_max_file_size() * 0.85)  # margin for estimate error
    return max(1, safe_bytes // _BYTES_PER_LABEL_ESTIMATE)


def attach_batch_labels_pdf(label_data_list, docname, doctype, filename=None, on_progress=None):
    """Builds the batch PDF(s) straight to temp files (build_batch_labels_pdf_file
    -- bounded memory regardless of label count) and attaches them, splitting
    into multiple numbered files if one file's worth of labels would exceed
    the site's max upload size (see _labels_per_output_file). Replaces any
    earlier batch_labels_{docname}*.pdf already on the doc so repeated
    retries (the "Generate Attachment" salvage button included) don't pile
    up duplicates -- including a previous run's file count differing from
    this one's (e.g. re-salvaging after adding more bunches).

    on_progress(done, total), if given, is forwarded to
    build_batch_labels_pdf_file for each output file in turn, offset so it
    reads as one continuous count across the WHOLE batch rather than
    resetting to 0 at each file split.

    Returns the list of new files' URLs (empty if attaching failed --
    logged either way; this must never itself raise, since the callers are
    background jobs that still need to notify the user either way).
    """
    import tempfile

    base_name = filename or f"batch_labels_{docname}"
    base_name = base_name[:-4] if base_name.endswith(".pdf") else base_name

    for existing in frappe.get_all(
        "File",
        filters={"attached_to_doctype": doctype, "attached_to_name": docname, "file_name": ["like", f"{base_name}%"]},
        pluck="name",
    ):
        frappe.delete_doc("File", existing, ignore_permissions=True, force=True)

    per_file = _labels_per_output_file()
    groups = [label_data_list[i:i + per_file] for i in range(0, len(label_data_list), per_file)] or [[]]
    total_parts = len(groups)
    total_labels = len(label_data_list)
    labels_done_before = 0

    file_urls = []
    try:
        for part, group in enumerate(groups, start=1):
            part_name = f"{base_name}.pdf" if total_parts == 1 else f"{base_name}_part{part}_of_{total_parts}.pdf"

            group_base = labels_done_before

            def _offset_progress(done, _group_total, base=group_base):
                if on_progress:
                    on_progress(base + done, total_labels)

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name
                build_batch_labels_pdf_file(group, tmp_path, on_progress=_offset_progress if on_progress else None)
                labels_done_before += len(group)

                with open(tmp_path, "rb") as f:
                    file_doc = frappe.get_doc({
                        "doctype": "File",
                        "file_name": part_name,
                        "attached_to_doctype": doctype,
                        "attached_to_name": docname,
                        "is_private": 0,
                        "content": f.read(),
                    })
                file_doc.insert(ignore_permissions=True)
                frappe.db.commit()
                file_urls.append(file_doc.file_url)
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        frappe.log_error(f"PDF saved successfully: {file_urls}", "PDF Save Success")
        return file_urls

    except Exception as e:
        frappe.log_error(f"Error saving PDF attachment: {str(e)}\n{frappe.get_traceback()}", "PDF Attachment Error")
        return file_urls  # whatever parts succeeded before the failure


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