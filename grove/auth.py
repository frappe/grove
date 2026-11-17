# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

from grove.api.control import CONTROL_ROLE

ALLOWED = "/api/method/grove.api."


def hook():
	"""`auth_hooks`, once the caller is known: a control client reaches `grove.api` and nothing
	else of the site — no `/api/resource`, no `frappe.client`, no Desk button. Its role holds write
	on Grove Team and Grove API Key so that its endpoints run under permission checks; this keeps
	that write to what the endpoints do with it, and the operator's fields (`suspended_at_grove`,
	`max_keys`) out of its reach. An operator who holds the role as well is not kept to it."""
	roles = frappe.get_roles()
	if CONTROL_ROLE not in roles or "System Manager" in roles:
		return
	# `cmd` outranks the path in Frappe's router: it names what runs.
	path = f"/api/method/{frappe.form_dict.cmd}" if frappe.form_dict.cmd else frappe.request.path
	if not path.startswith(ALLOWED):
		frappe.throw("Access not allowed for this URL", frappe.PermissionError)
