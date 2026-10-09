# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Who may call which Model. Every group a key belongs to grants, its own Allow adds, its Deny
removes, and nothing else is reachable.

The precedence is NOT applied here: Grove pushes each group and each key as separate Redis
records, and the GATEWAY resolves the two at request time. That is what stops a one-row edit on a
group from invalidating every key beneath it."""

import frappe


def model_rows(parenttype, parents=None):
	"""{parent: {parentfield: [model key, ...]}}. A row links a doc; what it grants is the key —
	one grant reaches the id in every geography that serves it."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all(
		"Grove Model Row", filters=filters, fields=["parent", "model_key", "parentfield"]
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, {}).setdefault(row.parentfield, []).append(row.model_key)
	for fields in grouped.values():
		for models in fields.values():
			models.sort()
	return grouped


def group_rows(parents=None):
	"""{Grove API Key: [group, ...]}, sorted."""
	filters = {"parenttype": "Grove API Key"}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Model Group Row", filters=filters, fields=["parent", "model_group"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, set()).add(row.model_group)
	return {parent: sorted(names) for parent, names in grouped.items()}


def limit_rows():
	"""{Grove API Key: ["requests:1m:200", ...]}, sorted: each entry as the gateway reads it."""
	rows = frappe.get_all(
		"Model Limit", filters={"parenttype": "Grove API Key"}, fields=["parent", "metric", "window", "value"]
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, []).append(f"{row.metric}:{row.window}:{row.value}")
	return {parent: sorted(entries) for parent, entries in grouped.items()}


def get_reachable_models(api_key):
	"""What one key may call, resolved the way the gateway does it: every group's grant and its
	own Allow, less its Deny. For showing a person their models; the gateway never reads this."""
	own = model_rows("Grove API Key", [api_key]).get(api_key, {})
	groups = group_rows([api_key]).get(api_key, [])
	granted = set(own.get("allow", []))
	for group in model_rows("Model Group", groups).values() if groups else ():
		granted.update(group.get("models", []))
	return sorted(granted - set(own.get("deny", [])))


def model_doc(model_key, geography):
	"""The doc under `model_key` that `geography` serves, for a grant that names an id: the
	vendor's record there, or ours. Unknown is the caller's error."""
	served_here = {"geography": geography, "provider_is_self_hosted": 1}
	names = frappe.get_all("Model", filters={"model_key": model_key}, or_filters=served_here, pluck="name")
	if not names:
		frappe.throw(f"No model is keyed {model_key!r} in {geography}.", frappe.DoesNotExistError)
	return names[0]


def validate_model_geography(rows, geography):
	"""A vendor's doc is one geography's, so a grant row names the doc, and the price, reached
	there. Ours serve wherever a replica runs."""
	models = frappe.get_all(
		"Model",
		filters={"name": ("in", [row.model for row in rows]), "provider_is_self_hosted": 0},
		fields=["model_key", "geography"],
	)
	elsewhere = sorted(m.model_key for m in models if m.geography != geography)
	if elsewhere:
		frappe.throw(f"{', '.join(elsewhere)} is not a model in {geography}.")
