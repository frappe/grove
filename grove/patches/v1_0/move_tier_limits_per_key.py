import frappe


def execute():
	"""Until a tier could say, the gateway counted its limits per key, every model together. They
	stay so: a tier's rows naming no model move to its per-key table and its scope says Key. A tier
	already holding per-key rows is left as it is. The save re-pushes each team's record."""
	for name in frappe.get_all("Tier", pluck="name"):
		tier = frappe.get_doc("Tier", name)
		if tier.key_limits:
			continue
		moved = [row for row in tier.limits if not row.model]
		tier.limit_scope = "Key"
		tier.set("key_limits", [{f: row.get(f) for f in ("metric", "window", "value")} for row in moved])
		tier.set("limits", [row for row in tier.limits if row.model])
		tier.save()
