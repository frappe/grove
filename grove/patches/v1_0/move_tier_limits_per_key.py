import frappe


def execute():
	"""Until a tier could say, the gateway counted its limits per key. They stay so: every tier's
	rows move to its per-key table and its scope says Key. The stamp re-pushes each team's record."""
	frappe.db.set_value(
		"Model Limit", {"parenttype": "Tier", "parentfield": "limits"}, "parentfield", "key_limits"
	)
	frappe.db.set_value("Tier", {"name": ("is", "set")}, "limit_scope", "Key")
