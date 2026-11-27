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

# A tier's limit scope and the table that holds its rows, on the Tier and on a Grove Team's own:
# Model Limit rows per model for a team, Key Limit rows naming no model for a key.
LIMIT_TABLES = {"Team": "limits", "Key": "key_limits"}
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


def limit_entries(parenttype, parents=None):
	"""{(parent, table): [{model_key, geography, metric, window, value}, ...]} of the limit rows on
	every `parenttype` doc, or on `parents`, sorted; `table` is `limits` (Model Limit) or
	`key_limits` (Key Limit, no model). `geography` is the row's model doc's, where the row holds;
	blank on a row naming no model or a model of ours, which hold everywhere."""
	filters = {"parenttype": parenttype}
	if parents is not None:
		filters["parent"] = ("in", list(parents))
	fields = ["parent", "parentfield", "metric", "window", "value"]
	per_model, per_key = {**filters, "parentfield": "limits"}, {**filters, "parentfield": "key_limits"}
	rows = frappe.get_all("Model Limit", filters=per_model, fields=[*fields, "model", "model_key"])
	rows += frappe.get_all("Key Limit", filters=per_key, fields=fields)
	geographies = model_geographies(row.model for row in rows)
	grouped = {}
	for row in rows:
		row.geography = geographies.get(row.model)
		grouped.setdefault((row.parent, row.parentfield), []).append(row)
	return {table: sorted_limits(entries) for table, entries in grouped.items()}


def limits_reaching(rows, geography):
	"""The limit rows that hold for a key in `geography`: those naming no model, a model of ours,
	or a model of that geography."""
	return [row for row in rows if not row["geography"] or row["geography"] == geography]


def limit_scopes(teams):
	"""{team: "Team" | "Key"} for `teams` ({team: tier}; blank is the default tier): who its tier
	counts rate limits for, the team as a whole or each of its keys. No tier at all: Team."""
	scopes = dict(frappe.get_all("Tier", fields=["name", "limit_scope"], as_list=True))
	fallback = default_tier()
	return {team: scopes.get(named or fallback) or "Team" for team, named in teams.items()}


def limits_in_force(teams):
	"""{team: [{model_key, geography, metric, window, value}, ...]} for `teams` ({team: tier}; blank
	is the default tier): the rows of the table its tier's `limit_scope` names, with the team's own
	rows of that table laid over them, a team row standing in for the tier's on its model, metric
	and window. A value of 0 is no limit there. A Key scope's rows name no model: they count every
	model together. A tier with no rows is uncapped; no tier at all: DEFAULT_LIMITS."""
	scopes = limit_scopes(teams)
	own, by_tier = limit_entries("Grove Team", list(teams)), limit_entries("Tier")
	fallback = default_tier()
	rows = {}
	for team, named in teams.items():
		tier, table = named or fallback, LIMIT_TABLES[scopes[team]]
		base = by_tier.get((tier, table), []) if tier else sorted_limits(DEFAULT_LIMITS)
		slots = {limit_slot(row): row for row in [*base, *own.get((team, table), [])]}
		rows[team] = sorted(slots.values(), key=limit_slot)
	return rows


def limit_slot(row):
	return (row["model_key"], row["geography"], row["metric"], row["window"])


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
		key=limit_slot,
	)


def validate_limit_rows(rows):
	"""One limit per model, metric and window, none below zero (0 is no limit there), a model named
	by its doc. A row may name any geography's model; a Key Limit row names none."""
	seen = set()
	for row in rows:
		model, model_key = row.get("model") or "", row.get("model_key")
		if (row.value or 0) < 0:
			frappe.throw(f"Limit row {row.idx}: the value cannot be below zero.")
		if model_key and not model:
			frappe.throw(f"Limit row {row.idx}: link the model, {model_key!r} is only its key.")
		if (model, row.metric, row.window) in seen:
			frappe.throw(
				f"Two limits on {row.metric} per {row.window}" + (f" for {model_key}." if model else ".")
			)
		seen.add((model, row.metric, row.window))


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
