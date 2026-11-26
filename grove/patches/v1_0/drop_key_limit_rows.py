import frappe


def execute():
	"""A key holds no limit rows of its own any more: per-key limits are its team's or its tier's
	`key_limits`. The rows the keys left behind go."""
	frappe.db.delete("Model Limit", {"parenttype": "Grove API Key"})
