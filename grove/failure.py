"""Announce a background failure instead of leaving it in the RQ job: a toast and a notification,
since work runs in workers and the caller has moved on. `reports_failure` covers a job that raises,
`AnsiblePlay.on_update` a playbook that returns non-zero, and `PodProvisioner.fail` calls `report`
itself. Reporting decides nothing: `reports_failure` re-raises, so the job still fails."""

import functools

import frappe
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

IN_PROGRESS = ("Installing", "Provisioning")


def report(doctype, name, title, detail, *, mark_broken=False):
	"""A toast reaches whoever is still on the page, a notification whoever has left it. Neither is
	durable: next week it is the Error Log and the doc's Ansible Plays."""
	if not doctype or not name:
		return
	frappe.local.grove_failure_reported = True

	if mark_broken:
		_mark_broken(doctype, name)

	_toast(doctype, name, title, detail)
	_notify(doctype, name, title, detail)


def reports_failure(mark_broken=False, doctype=None):
	"""Reports, then RE-RAISES. A bound method finds the doc on `self`; a module-level job takes the
	docname first and names its doctype here."""

	def decorate(method):
		@functools.wraps(method)
		def wrapper(*args, **kwargs):
			try:
				return method(*args, **kwargs)
			except Exception as error:
				if not getattr(frappe.local, "grove_failure_reported", False):
					failed = args[0] if args else None
					report(
						doctype or getattr(failed, "doctype", None),
						failed if doctype else getattr(failed, "name", None),
						f"{_label(method)} failed",
						str(error),
						mark_broken=mark_broken,
					)
				raise

		return wrapper

	return decorate


def _mark_broken(doctype, name):
	"""Only from a transitional status, so a dead provision stops looking like a slow one. Written
	with db.set_value to match every other status write on these paths, which stay outside the
	document lifecycle so a provision does not re-trigger a sync."""
	meta = frappe.get_meta(doctype)
	status_field = meta.get_field("status")
	if not status_field or "Broken" not in (status_field.options or "").split("\n"):
		return
	if frappe.db.get_value(doctype, name, "status") not in IN_PROGRESS:
		return
	frappe.db.set_value(doctype, name, "status", "Broken")
	frappe.db.commit()


def _toast(doctype, name, title, detail):
	"""execute_job sets the session to whoever enqueued the job, so this lands on the person who
	clicked the button with no room or subscription to arrange."""
	try:
		frappe.msgprint(
			f"{name}: {detail}" if detail else name,
			title=title,
			indicator="red",
			alert=True,
			realtime=True,
		)
	except Exception:
		frappe.log_error(title=f"Could not toast a failure on {doctype} {name}")


def _notify(doctype, name, title, detail):
	"""type="Alert" is load-bearing: make_notification_logs drops a notification whose recipient is
	also its sender for every other type, and someone provisioning their own box is exactly that.
	Administrator is skipped by the framework, so a job with no real session — the scheduler —
	produces no notification here: it is why the scheduled syncs are not wired into this."""
	user = frappe.session.user
	if not user or user == "Guest":
		return

	try:
		enqueue_create_notification(
			user,
			{
				"type": "Alert",
				"document_type": doctype,
				"document_name": name,
				"subject": f"{title}: <b class='subject-title'>{name}</b>",
				"email_content": detail,
				"from_user": user,
			},
		)
	except Exception:
		frappe.log_error(title=f"Could not notify a failure on {doctype} {name}")


def _label(method):
	return method.__name__.lstrip("_").replace("_", " ").capitalize()
