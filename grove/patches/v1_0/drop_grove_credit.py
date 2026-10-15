"""The prepaid ledger is gone: Grove Credit, its allocations, and the Central Team columns that
carried a balance. The rows held no real money. A discrepancy resolved before `resolved_on`
existed is dated by its last change (site time, and that day)."""

import frappe


def execute():
	for doctype in ("Grove Credit Allocation", "Grove Credit"):
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
	for column in ("email", "balance", "credit_exhausted"):
		if frappe.db.has_column("Central Team", column):
			frappe.db.sql_ddl(f"alter table `tabCentral Team` drop column `{column}`")
	frappe.db.sql(
		"""update `tabCredit Discrepancy` set resolved_on = modified, resolved_day = date(modified)
		where ifnull(resolution, '') != '' and resolved_on is null"""
	)
