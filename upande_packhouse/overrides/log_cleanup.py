"""Log Settings can clear Version, Deleted Document and Notification Log, a batch at a time.

Log Settings clears every listed log inside one daily scheduled job, which the default queue stops after a few
minutes. Version has no clear_old_logs, so it could not be listed at all, and Deleted Document's and
Notification Log's delete every old row in one statement -- on a backlog of millions that outlives the job, rolls back and the logs listed after
it are never cleared. Here each call deletes oldest-first in small committed batches and stops at its time
budget; a backlog shrinks over a few nights instead of stalling the job."""

import time

import frappe
from frappe.core.doctype.deleted_document.deleted_document import DeletedDocument
from frappe.core.doctype.version.version import Version
from frappe.desk.doctype.notification_log.notification_log import NotificationLog

BATCH = 5000
BUDGET_SEC = 60


def clear_in_batches(doctype, days, budget=BUDGET_SEC):
	cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -int(days))
	stop = time.monotonic() + budget
	cleared = 0
	while time.monotonic() < stop:
		names = frappe.get_all(
			doctype,
			filters={"creation": ("<", cutoff)},
			order_by="creation asc",
			limit=BATCH,
			pluck="name",
		)
		if not names:
			break
		frappe.db.delete(doctype, {"name": ("in", names)})
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- each batch stands alone, so a stopped job keeps what it cleared
		cleared += len(names)
	return cleared


class VersionLogClearing(Version):
	@staticmethod
	def clear_old_logs(days=180):
		return clear_in_batches("Version", days)


class DeletedDocumentLogClearing(DeletedDocument):
	@staticmethod
	def clear_old_logs(days=180):
		return clear_in_batches("Deleted Document", days)


class NotificationLogClearing(NotificationLog):
	@staticmethod
	def clear_old_logs(days=180):
		return clear_in_batches("Notification Log", days)
