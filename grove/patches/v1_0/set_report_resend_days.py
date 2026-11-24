"""Grove Settings predates Resend Days, so its default never reached an existing site."""

import frappe


def execute():
	if frappe.db.get_single_value("Grove Settings", "report_resend_days"):
		return
	default = frappe.get_meta("Grove Settings").get_field("report_resend_days").default
	frappe.db.set_single_value("Grove Settings", "report_resend_days", int(default))
