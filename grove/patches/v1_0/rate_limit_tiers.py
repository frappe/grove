"""A key under exactly DEFAULT_LIMITS loses its rows and runs under its team's Tier; a
key an operator pinned to anything else keeps its rows."""

import frappe

from grove.access import DEFAULT_LIMITS


def execute():
	rows = frappe.get_all(
		"Model Limit", filters={"parenttype": "Grove API Key"}, fields=["parent", "metric", "window", "value"]
	)
	by_key = {}
	for row in rows:
		by_key.setdefault(row.parent, set()).add((row.metric, row.window, row.value))
	default = {(limit["metric"], limit["window"], limit["value"]) for limit in DEFAULT_LIMITS}
	inheriting = [key for key, limits in by_key.items() if limits == default]
	if inheriting:
		frappe.db.delete("Model Limit", {"parenttype": "Grove API Key", "parent": ("in", inheriting)})
