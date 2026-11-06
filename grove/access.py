# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Who may call which Model. The policy is the team's: every group it is in grants, its Allow
adds, its Deny removes, and nothing else is reachable. What is one geography's — a Model Group, a
vendor's model doc — reaches only the team's keys in that geography; a model of ours reaches them
all.

The precedence is NOT applied here: Grove pushes each group and each key as separate Redis
records, and the GATEWAY resolves the two at request time. That is what stops a one-row edit on a
group from invalidating every key beneath it."""

import frappe
from frappe.utils import flt

DEFAULT_LIMITS = (
	{"metric": "requests", "window": "1m", "value": 20},
	{"metric": "total_tokens", "window": "1m", "value": 100_000},
)


def default_tier():
	"""The Tier a team with none runs under, and the one an unseen name is cloned from; None when
	Grove Settings names none."""
	return frappe.db.get_single_value("Grove Settings", "default_tier") or None


def team_limits(team, for_update=False):
	"""What gates `team`'s keys: `suspended`, `log_payloads`, `key_cap_window` (what each cap
	counts: the month, or everything since the mint), its `tier`, and the tier's `spend_limit` (the
	default tier's when it has none; without either, 0: no cap can be set). `for_update` locks the
	team row for a cap check. The tier comes off the doc cache: a save clears it."""
	fields = ["suspended", "log_payloads", "key_cap_window", "tier"]
	row = (
		frappe.db.get_value("Grove Team", team, fields, as_dict=True, for_update=for_update) or frappe._dict()
	)
	tier = row.tier or default_tier()
	row.spend_limit = flt(tier and frappe.get_cached_value("Tier", tier, "spend_limit"))
	return row


def model_rows(parenttype, parents=None):
	"""{parent: {parentfield: [model key, ...]}}. A row links a doc; what it grants is the key —
	one grant reaches the id in every geography that serves it."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Grove Model Row", filters=filters, fields=["parent", "model_key", "parentfield"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, {}).setdefault(row.parentfield, []).append(row.model_key)
	for fields in grouped.values():
		for models in fields.values():
			models.sort()
	return grouped


def group_rows(parents=None, parenttype="Grove Team"):
	"""{parent: [group, ...]}, sorted: the groups every Grove Team, or every Grove API Key, is in."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all("Model Group Row", filters=filters, fields=["parent", "model_group"])
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, set()).add(row.model_group)
	return {parent: sorted(names) for parent, names in grouped.items()}


def group_geographies():
	"""{group: its geography}: which of a team's keys the group reaches."""
	return {g.name: g.geography for g in frappe.get_all("Model Group", fields=["name", "geography"])}


def team_grants(teams=None):
	"""{team: {parentfield: [(model key, geography), ...]}} off the Allow and Deny rows of every
	Grove Team, or of `teams`. The geography is the model doc's: a vendor's record is one
	geography's; a model of ours has none, and reaches a key in any."""
	filters = {"parenttype": "Grove Team"}
	if teams is not None:
		filters["parent"] = ("in", list(teams))
	rows = frappe.get_all(
		"Grove Model Row", filters=filters, fields=["parent", "parentfield", "model", "model_key"]
	)
	geographies = {}
	if rows:
		docs = frappe.get_all(
			"Model", filters={"name": ("in", [row.model for row in rows])}, fields=["name", "geography"]
		)
		geographies = {doc.name: doc.geography for doc in docs}
	grouped = {}
	for row in rows:
		pair = (row.model_key, geographies.get(row.model) or "")
		grouped.setdefault(row.parent, {}).setdefault(row.parentfield, []).append(pair)
	return grouped


def reaching(rows, geography):
	"""The model keys among `rows` ((key, geography) pairs) a key in `geography` is granted: those
	of its geography and ours, sorted."""
	return sorted({key for key, where in rows if not where or where == geography})


def limit_entries(parenttype, parents=None):
	"""{parent: [{model_key, metric, window, value}, ...]} of the Model Limit rows on every
	`parenttype` doc, or on `parents`, each list sorted by model, metric and window."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all(
		"Model Limit", filters=filters, fields=["parent", "model_key", "metric", "window", "value"]
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, []).append(row)
	return {parent: sorted_limits(entries) for parent, entries in grouped.items()}


def tier_limits(teams):
	"""{team: [{model_key, metric, window, value}, ...]} for `teams` ({team: tier}; blank is the
	default tier; a tier with no rows is uncapped), else DEFAULT_LIMITS when the site has no tier at
	all. The rows the gateway counts under the team, per model; a key's own rows travel beside them."""
	by_tier = limit_entries("Tier")
	fallback = default_tier()
	default = sorted_limits(DEFAULT_LIMITS)
	return {
		team: by_tier.get(tier, []) if (tier := named or fallback) else default for team, named in teams.items()
	}


def tiers_without_limit(model_key):
	"""The Tiers with no limit row naming `model_key`. A model is published only once every tier
	says what a team on it may do with it: the gate in `Model.validate_publishable`."""
	named = frappe.get_all("Model Limit", filters={"parenttype": "Tier", "model_key": model_key}, pluck="parent")
	return sorted(set(frappe.get_all("Tier", pluck="name")) - set(named))


def sorted_limits(rows):
	return sorted(
		(
			{"model_key": r.get("model_key") or "", "metric": r.get("metric"), "window": r.get("window"), "value": r.get("value")}
			for r in rows
		),
		key=lambda r: (r["model_key"], r["metric"], r["window"]),
	)


def validate_limit_rows(rows):
	"""One limit per model, metric and window, each above zero, each named model one Grove serves
	somewhere; the gateway refuses a push carrying one it cannot read. The same rule on a key's own
	rows and on a Tier's."""
	seen = set()
	for row in rows:
		if (row.value or 0) <= 0:
			frappe.throw(f"Limit row {row.idx}: the value must be above zero.")
		if row.model_key and not frappe.db.exists("Model", {"model_key": row.model_key}):
			frappe.throw(f"Limit row {row.idx}: no model has the key {row.model_key!r}.")
		if (row.model_key or "", row.metric, row.window) in seen:
			frappe.throw(f"Two limits on {row.metric} per {row.window}" + (f" for {row.model_key}." if row.model_key else "."))
		seen.add((row.model_key or "", row.metric, row.window))


def get_reachable_models(team, geography):
	"""What a key of `team` in `geography` may call off the team's policy alone: every grant of the
	team's groups there and the Allow that reaches there, less the Deny that does. A key's own
	groups are not counted. For showing a person their models; the gateway never reads this."""
	where = group_geographies()
	groups = [g for g in group_rows([team]).get(team, []) if where.get(g) == geography]
	granted = set()
	for group in model_rows("Model Group", groups).values() if groups else ():
		granted.update(group.get("models", []))
	own = team_grants([team]).get(team, {})
	granted.update(reaching(own.get("allow", []), geography))
	return sorted(granted - set(reaching(own.get("deny", []), geography)))


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
