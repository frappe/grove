# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

ALLOWED = "/api/method/grove.api."


def hook():
	"""`auth_hooks`, once the caller is known: only an operator (System Manager) has the run of the
	site. Anyone else signed in, a control client or not, reaches `grove.api` and nothing else: no
	`/api/resource`, no `frappe.client`, no Desk button, no page. A guest is left to Frappe, which
	lets one call only what is whitelisted for guests."""
	if frappe.session.user == "Guest" or "System Manager" in frappe.get_roles():
		return
	# `cmd` outranks the path in Frappe's router: it names what runs.
	path = f"/api/method/{frappe.form_dict.cmd}" if frappe.form_dict.cmd else frappe.request.path
	if not path.startswith(ALLOWED):
		frappe.throw("Access not allowed for this URL", frappe.PermissionError)
