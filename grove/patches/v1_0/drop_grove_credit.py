"""The prepaid ledger is gone: Grove Credit, its allocations, and the Grove Team columns that
carried a balance. The rows held no real money. A discrepancy resolved before `resolved_on`
existed is dated by its last change (site time)."""

import frappe


def execute():
	for doctype in ("Grove Credit Allocation", "Grove Credit"):
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
	for column in ("email", "balance", "credit_exhausted"):
		if frappe.db.has_column("Grove Team", column):
			frappe.db.sql_ddl(f"alter table `tabGrove Team` drop column `{column}`")
	frappe.db.sql(
		"""update `tabCost Discrepancy` set resolved_on = modified
		where ifnull(resolution, '') != '' and resolved_on is null"""
	)
