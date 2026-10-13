# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Who may call which Model. Every group a key belongs to grants, its own Allow adds, its Deny
removes, and nothing else is reachable.

The precedence is NOT applied here: Grove pushes each group and each key as separate Redis
records, and the GATEWAY resolves the two at request time. That is what stops a one-row edit on a
group from invalidating every key beneath it."""

import frappe
from frappe.utils import flt

# What a key runs under when neither it, its team's Tier nor the default tier says otherwise.
DEFAULT_LIMITS = (
	{"metric": "requests", "window": "1m", "value": 20},
	{"metric": "total_tokens", "window": "1m", "value": 100_000},
)


def default_tier():
	"""The Tier a team with none runs under, and the one an unseen name is cloned from; None when
	Grove Settings names none."""
	return frappe.db.get_single_value("Grove Settings", "default_tier") or None


def team_limits(team, for_update=False):
	"""What gates `team`'s keys: `free`, `suspended`, its `tier`, and the tier's `max_keys` /
	`spend_limit` (the default tier's when it has none; without either, 0 and 0: no key limit, and
	no cap can be set). `max_keys` is the team's own under Override Max Keys. `for_update` locks
	the team row for a cap check."""
	fields = ["free", "suspended", "tier", "override_max_keys", "max_keys"]
	row = frappe.db.get_value("Central Team", team, fields, as_dict=True, for_update=for_update) or frappe._dict()
	tier = row.tier or default_tier()
	limits = (tier and frappe.db.get_value("Tier", tier, ["max_keys", "spend_limit"], as_dict=True)) or frappe._dict()
	if not row.override_max_keys:
		row.max_keys = limits.max_keys
	row.max_keys, row.spend_limit = row.max_keys or 0, flt(limits.spend_limit)
	return row


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


def limit_entries(parenttype, parents=None):
	"""{parent: [{metric, window, value}, ...]} of the Model Limit rows on every `parenttype` doc, or
	on `parents`, each list sorted by metric and window."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Model Limit", filters=filters, fields=["parent", "metric", "window", "value"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, []).append({"metric": row.metric, "window": row.window, "value": row.value})
	return {parent: sorted_limits(entries) for parent, entries in grouped.items()}


def effective_limits(keys, own, tiers):
	"""{key: [{metric, window, value}, ...]} for `keys` (rows with `name` and `team`), as the gateway
	is to count them: the key's own rows (`own`, from `limit_entries`), else the rows of its team's
	Tier (`tiers`: {team: tier}; blank is the default tier; a tier with no rows is uncapped), else
	DEFAULT_LIMITS."""
	by_tier = limit_entries("Tier")
	fallback = default_tier()
	default = sorted_limits(DEFAULT_LIMITS)
	resolved = {}
	for key in keys:
		if key.name in own:
			resolved[key.name] = own[key.name]
		elif tier := tiers.get(key.team) or fallback:
			resolved[key.name] = by_tier.get(tier, [])
		else:
			resolved[key.name] = default
	return resolved


def sorted_limits(rows):
	return sorted(({"metric": r["metric"], "window": r["window"], "value": r["value"]} for r in rows), key=lambda r: (r["metric"], r["window"]))


def validate_limit_rows(rows):
	"""One limit per metric and window, each above zero; the gateway refuses a push carrying one it
	cannot read. The same rule on a key's own rows and on a Tier's."""
	seen = set()
	for row in rows:
		if (row.value or 0) <= 0:
			frappe.throw(f"Limit row {row.idx}: the value must be above zero.")
		if (row.metric, row.window) in seen:
			frappe.throw(f"Two limits on {row.metric} per {row.window}.")
		seen.add((row.metric, row.window))


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
