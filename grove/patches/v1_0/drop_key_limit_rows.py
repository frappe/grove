import frappe


def execute():
	"""A key holds no limit rows of its own any more; the rows it left behind go."""
	frappe.db.delete("Model Limit", {"parenttype": "Grove API Key"})
