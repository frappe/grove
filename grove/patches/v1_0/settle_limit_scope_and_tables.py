import frappe


def execute():
	"""For a site that took the limit scope before it settled: the option Team is Model, and per-key
	rows it kept as Model Limit rows become Key Limit rows. One naming a model has no per-key table
	to be in, so it goes back to the per-model one."""
	frappe.db.set_value("Tier", {"limit_scope": "Team"}, "limit_scope", "Model")
	stranded = frappe.get_all(
		"Model Limit",
		filters={"parentfield": "key_limits"},
		fields=["name", "parenttype", "parent", "model", "metric", "window", "value"],
		order_by="parent, idx",
	)
	parents = {}
	for row in stranded:
		if row.model:
			frappe.db.set_value("Model Limit", row.name, "parentfield", "limits")
		else:
			parents.setdefault((row.parenttype, row.parent), []).append(row)
	for (doctype, name), rows in parents.items():
		doc = frappe.get_doc(doctype, name)
		if not doc.key_limits:
			doc.set(
				"key_limits", [{field: row[field] for field in ("metric", "window", "value")} for row in rows]
			)
			doc.save()
	frappe.db.delete("Model Limit", {"parentfield": "key_limits"})
