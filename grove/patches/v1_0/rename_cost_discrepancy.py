import frappe


def execute():
	if frappe.db.exists("DocType", "Credit Discrepancy") and not frappe.db.exists("DocType", "Cost Discrepancy"):
		frappe.rename_doc("DocType", "Credit Discrepancy", "Cost Discrepancy", force=True)
	for doctype in ("Usage Correction", "Gateway Spend Adjustment"):
		if frappe.db.has_column(doctype, "credit_discrepancy"):
			frappe.db.rename_column(doctype, "credit_discrepancy", "cost_discrepancy")
