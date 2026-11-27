import frappe


def execute():
	"""The gateway counted limits per key, every model together, before a tier could say. They stay
	so; a tier already holding per-key rows is left as it is."""
	for name in frappe.get_all("Tier", pluck="name"):
		tier = frappe.get_doc("Tier", name)
		if tier.key_limits:
			continue
		moved = [row for row in tier.limits if not row.model]
		tier.limit_scope = "Key"
		tier.set("key_limits", [{f: row.get(f) for f in ("metric", "window", "value")} for row in moved])
		tier.set("limits", [row for row in tier.limits if row.model])
		tier.save()
