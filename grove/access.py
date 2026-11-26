# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Who may call which Model. The policy is the team's: every group it is in grants, its Allow
adds, its Deny removes, and nothing else is reachable. What is one geography's — a Model Group, a
vendor's model doc — reaches only the team's keys in that geography; a model of ours reaches them
all. The precedence is NOT applied here: Grove pushes each group and each key as separate Redis
records, and the GATEWAY resolves the two at request time. That is what stops a one-row edit on a
group from invalidating every key beneath it."""

import frappe
from frappe.utils import flt

DEFAULT_LIMITS = (
	{"metric": "requests", "window": "1m", "value": 20},
	{"metric": "total_tokens", "window": "1m", "value": 100_000},
)


def default_tier():
	"""The Tier a team with none runs under, and the one an unseen name is cloned from."""
	return frappe.db.get_single_value("Grove Settings", "default_tier") or None


def team_limits(team, for_update=False):
	"""What gates `team`'s keys: `key_cap_window` (what each cap counts: the month, or everything
	since the mint) and the tier's `spend_limit` (the default tier's when it has none; without
	either, 0: no cap can be set). The tier comes off the doc cache: a save clears it."""
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
	geographies = model_geographies(row.model for row in rows)
	grouped = {}
	for row in rows:
		pair = (row.model_key, geographies.get(row.model) or "")
		grouped.setdefault(row.parent, {}).setdefault(row.parentfield, []).append(pair)
	return grouped


def reaching(rows, geography):
	"""The model keys among `rows` ((key, geography) pairs) a key in `geography` is granted: those
	of its geography and ours, sorted."""
	return sorted({key for key, where in rows if not where or where == geography})


def model_geographies(models):
	"""{model doc: its geography} for the docs named; a model of ours has none."""
	names = sorted({model for model in models if model})
	if not names:
		return {}
	docs = frappe.get_all("Model", filters={"name": ("in", names)}, fields=["name", "geography"])
	return {doc.name: doc.geography for doc in docs}


def model_reaching(model_key, geography):
	"""The doc of `model_key` a key in `geography` reaches: that geography's, or one of ours."""
	docs = frappe.get_all("Model", filters={"model_key": model_key}, fields=["name", "geography"])
	return next((doc.name for doc in docs if not doc.geography or doc.geography == geography), None)


def limit_entries(parenttype, parents=None):
	"""{parent: [{model_key, geography, metric, window, value}, ...]} of the Model Limit rows on
	every `parenttype` doc, or on `parents`, sorted. `geography` is the row's model doc's, where the
	row holds; blank on a row naming no model or a model of ours, which hold everywhere."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	rows = frappe.get_all(
		"Model Limit", filters=filters, fields=["parent", "model", "model_key", "metric", "window", "value"]
	)
	geographies = model_geographies(row.model for row in rows)
	grouped = {}
	for row in rows:
		row.geography = geographies.get(row.model)
		grouped.setdefault(row.parent, []).append(row)
	return {parent: sorted_limits(entries) for parent, entries in grouped.items()}


def limits_reaching(rows, geography):
	"""The limit rows that hold for a key in `geography`: those naming no model, a model of ours,
	or a model of that geography."""
	return [row for row in rows if not row["geography"] or row["geography"] == geography]


def tier_limits(teams):
	"""{team: [{model_key, geography, metric, window, value}, ...]} for `teams` ({team: tier}; blank
	is the default tier; a tier with no rows is uncapped), else DEFAULT_LIMITS when the site has no
	tier at all. The rows the gateway counts under the team, per model, each where its
	geography says (`limits_reaching`); a key's own rows travel beside them."""
	by_tier = limit_entries("Tier")
	fallback = default_tier()
	default = sorted_limits(DEFAULT_LIMITS)
	return {
		team: by_tier.get(tier, []) if (tier := named or fallback) else default
		for team, named in teams.items()
	}


def sorted_limits(rows):
	return sorted(
		(
			{
				"model_key": r.get("model_key") or "",
				"geography": r.get("geography") or "",
				"metric": r.get("metric"),
				"window": r.get("window"),
				"value": r.get("value"),
			}
			for r in rows
		),
		key=lambda r: (r["model_key"], r["geography"], r["metric"], r["window"]),
	)


def validate_limit_rows(rows, geography=None):
	"""One limit per model, metric and window, each above zero, a model named by its doc. On a key,
	whose `geography` is given, the model is one the key reaches; a Tier's rows may name any."""
	where = model_geographies(row.model for row in rows) if geography else {}
	seen = set()
	for row in rows:
		if (row.value or 0) <= 0:
			frappe.throw(f"Limit row {row.idx}: the value must be above zero.")
		if row.model_key and not row.model:
			frappe.throw(f"Limit row {row.idx}: link the model, {row.model_key!r} is only its key.")
		if where.get(row.model) not in (None, "", geography):
			frappe.throw(
				f"Limit row {row.idx}: {row.model_key} is of {where[row.model]}, not this key's {geography}."
			)
		if (row.model or "", row.metric, row.window) in seen:
			frappe.throw(
				f"Two limits on {row.metric} per {row.window}"
				+ (f" for {row.model_key}." if row.model else ".")
			)
		seen.add((row.model or "", row.metric, row.window))


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
