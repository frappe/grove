# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""The Ops page: fleet-wide usage of teams, keys and models for operators, off the helpers the
control API reads, plus what needs a human (stuck usage, disputed costs), the last syncs, and
each vendor key's counters. Every read is System Manager's; `peek` alone dials the gateways."""

import frappe
from frappe.utils import add_days, cint, flt

from grove.access import team_limits
from grove.api.keys import key_rows
from grove.api.usage import usage_window
from grove.billing.doctype.usage_record.usage_record import money_rows
from grove.billing.pricing import committed, month_spent_by
from grove.pathway.key_stats import COUNTS, LAST
from grove.pathway.routes import utc_timestamp
from grove.utils import site_datetime, utc_today

ROLE = "System Manager"
USAGE_FIELDS = ("requests", "tokens", "cost")
SUMS = "sum(x.requests) as requests, sum(x.tokens) as tokens, sum(x.cost) as cost"
KEY_JOIN = "left join `tabGrove API Key` k on k.name = x.api_key"
DIMENSIONS = {
	"model": "x.model",
	"provider": "substring_index(x.model, '/', 1)",
	"team": "x.team",
	"geography": "coalesce(k.geography, '')",
}
TOP = 10
# ponytail: the 500 costliest keys of the window; page past that, the filters narrow it today.
KEY_LIMIT = 500


class Window:
	"""The page's filters as `money_rows` takes them: a UTC day range and {team, api_key, model}.
	`model` (a Model name) resolves to its model key; `provider` resolves to its model keys and
	`geography` to its key names (an empty resolution reads nothing, not everything). `narrowed`
	says a model-level filter is on, which the record-level gateway charge cannot follow."""

	def __init__(self, filters):
		given = frappe._dict(frappe.parse_json(filters) or {})
		self.from_date, self.to_date = self.days_of(given)
		self.narrowed = bool(given.model or given.provider)
		self.filters = {
			"team": given.team or None,
			"api_key": self.keys_of(given),
			"model": self.models_of(given),
		}

	def days_of(self, given):
		"""The UTC days asked for: Custom's own two, Today and Yesterday (the page's alone; the
		control API stops at Last 7 Days), else `usage_window`'s named period."""
		if given.period == "Custom":
			return usage_window(given.from_date, given.to_date, None, None)
		if given.period in ("Today", "Yesterday"):
			day = add_days(utc_today(), -1 if given.period == "Yesterday" else 0)
			return day, day
		return usage_window(None, None, given.period or "This Month", None)

	def keys_of(self, given):
		if not given.geography:
			return given.api_key or None
		filters = {"geography": given.geography}
		if given.team:
			filters["team"] = given.team
		if given.api_key:
			filters["name"] = given.api_key
		# ponytail: a geography's key names ride the IN list; a join in money_rows past lakhs.
		return frappe.get_list("Grove API Key", filters=filters, pluck="name") or [""]

	def models_of(self, given):
		if given.model:
			return frappe.db.get_value("Model", given.model, "model_key") or ""
		if not given.provider:
			return None
		providers = frappe.get_list("Model Provider", filters={"provider_name": given.provider}, pluck="name")
		return (
			frappe.get_list(
				"Model", filters={"provider": ("in", providers)}, pluck="model_key", distinct=True
			)
			if providers
			else []
		) or [""]

	@property
	def previous(self):
		"""The window of equal length ending the day before this one."""
		days = (self.to_date - self.from_date).days + 1
		return add_days(self.from_date, -days), add_days(self.from_date, -1)

	def table(self, from_date=None, to_date=None):
		return money_rows(
			self.filters, from_date=from_date or self.from_date, to_date=to_date or self.to_date
		)

	def gateway_charge(self, from_date, to_date):
		"""Σ `gateway_cost` of the records — what the boxes charged the keys, the figure the Cost
		Discrepancy audit holds against Grove's price. On the record alone, so team and key narrow
		it and a model does not."""
		conditions = ["r.day between %(from_date)s and %(to_date)s"]
		params = {"from_date": from_date, "to_date": to_date}
		for field in ("team", "api_key"):
			if (value := self.filters[field]) is not None:
				conditions.append(f"r.`{field}` {'in' if isinstance(value, list) else '='} %({field})s")
				params[field] = value
		total = frappe.db.sql(
			f"select sum(r.gateway_cost) from `tabUsage Record` r where {' and '.join(conditions)}", params
		)
		return flt(total[0][0], 9)


def operator():
	frappe.only_for(ROLE)


@frappe.whitelist()
@frappe.read_only()
def overview(filters: dict | str | None = None) -> dict:
	"""The window's totals beside the previous window's."""
	operator()
	window = Window(filters)
	return {
		"from_date": str(window.from_date),
		"to_date": str(window.to_date),
		"totals": totals(window, window.from_date, window.to_date),
		"previous": totals(window, *window.previous),
	}


def totals(window, from_date, to_date):
	table, params = window.table(from_date, to_date)
	row = frappe.db.sql(
		f"""select {SUMS}, count(distinct x.team) as teams, count(distinct x.api_key) as key_count,
		max(x.pulled_at) as as_of from {table}""",
		params,
		as_dict=True,
	)[0]
	charged = None if window.narrowed else window.gateway_charge(from_date, to_date)
	return {
		**usage_of(row),
		"gateway_cost": charged,
		"drift": None if charged is None else round(flt(row.cost, 9) - charged, 9),
		"teams": cint(row.teams),
		"keys": cint(row.key_count),
		"as_of": utc_timestamp(row.as_of) if row.as_of else None,
	}


def usage_of(row):
	"""requests, tokens, cost of one summed row; zeros for none."""
	if not row:
		return dict.fromkeys(USAGE_FIELDS, 0)
	return {"requests": cint(row["requests"]), "tokens": cint(row["tokens"]), "cost": flt(row["cost"], 9)}


@frappe.whitelist()
@frappe.read_only()
def trend(filters: dict | str | None = None, dimension: str = "model") -> list[dict]:
	"""Usage per UTC day and `dimension` (model, provider — the model key's prefix —, team, or
	the key's geography), in day order."""
	operator()
	if dimension not in DIMENSIONS:
		frappe.throw(f"Unknown dimension {dimension!r}: one of {', '.join(DIMENSIONS)}.")
	table, params = Window(filters).table()
	expression = DIMENSIONS[dimension]
	rows = frappe.db.sql(
		f"""select x.day, {expression} as label, {SUMS} from {table} {KEY_JOIN}
		group by x.day, {expression} order by x.day, {expression}""",
		params,
		as_dict=True,
	)
	return [{"day": str(row.day), "label": row.label or "", **usage_of(row)} for row in rows]


@frappe.whitelist()
@frappe.read_only()
def leaders(filters: dict | str | None = None) -> dict:
	"""The costliest teams, keys and models of the window, TOP of each."""
	operator()
	table, params = Window(filters).table()
	keys = grouped(table, params, "x.api_key", TOP)
	titles = key_titles([key["name"] for key in keys])
	for key in keys:
		key.update(titles.get(key["name"], {}))
	models = grouped(table, params, "x.model", TOP)
	docs = model_docs([model["name"] for model in models])
	for model in models:
		model["model_doc"] = docs.get(model["name"])
	return {"teams": grouped(table, params, "x.team", TOP), "keys": keys, "models": models}


def grouped(table, params, expression, limit=None):
	"""The window's usage summed by `expression`, costliest first, as rows with `name`."""
	tail = f" limit {int(limit)}" if limit else ""
	rows = frappe.db.sql(
		f"select {expression} as name, {SUMS} from {table} group by name order by cost desc, requests desc{tail}",
		params,
		as_dict=True,
	)
	return [{"name": row.name or "", **usage_of(row)} for row in rows]


def model_docs(model_keys):
	"""{model key: the first Model doc named by it}, the one the page's Model picker lists, so a
	key read off a leaderboard sets the same filter."""
	if not model_keys:
		return {}
	rows = frappe.get_list(
		"Model", filters={"model_key": ("in", model_keys)}, fields=["name", "model_key"], order_by="name"
	)
	docs = {}
	for row in rows:
		docs.setdefault(row.model_key, row.name)
	return docs


def key_titles(names):
	if not names:
		return {}
	rows = frappe.get_list(
		"Grove API Key", filters={"name": ("in", names)}, fields=["name", "title", "team", "masked"]
	)
	return {row.name: {"title": row.title, "team": row.team, "masked": row.masked} for row in rows}


@frappe.whitelist()
@frappe.read_only()
def teams(filters: dict | str | None = None) -> list[dict]:
	"""One row per team with usage in the window (the filtered team always): its tier's limits,
	live keys, this month's spend and what its caps commit, the last day it was served."""
	operator()
	window = Window(filters)
	table, params = window.table()
	usage = {row["name"]: row for row in grouped(table, params, "x.team")}
	names = set(usage) | ({window.filters["team"]} if window.filters["team"] else set())
	names.discard("")
	if not names:
		return []
	names = list(names)
	spent, live, used = month_spent_by("team", names), key_counts(names), last_used("team", names)
	rows = []
	for team in frappe.get_list(
		"Grove Team", filters={"name": ("in", names)}, fields=["name", "tier", "suspended", "key_cap_window"]
	):
		limits = team_limits(team.name)
		rows.append(
			{
				"team": team.name,
				"tier": team.tier,
				"suspended": bool(team.suspended),
				"key_cap_window": team.key_cap_window or "Monthly",
				"keys_live": live.get(team.name, 0),
				"max_keys": limits.max_keys,
				"month_spent": flt(spent.get(team.name, 0), 9),
				"spend_limit": None if team.key_cap_window == "Total" else flt(limits.spend_limit, 9),
				# ponytail: two queries a team; one grouped read past ~200 teams.
				"committed": committed(team.name, lock=False),
				"last_used": used.get(team.name),
				**usage_of(usage.get(team.name)),
			}
		)
	rows.sort(key=lambda row: -row["cost"])
	return rows


def key_counts(teams):
	rows = frappe.db.sql(
		"""select team, count(*) from `tabGrove API Key` where team in %(teams)s and status = 'active'
		group by team""",
		{"teams": teams},
	)
	return dict(rows)


def last_used(field, names):
	"""{name: the last UTC day a record was served} by `team` or `api_key`."""
	rows = frappe.db.sql(
		f"select `{field}`, max(day) from `tabUsage Record` where `{field}` in %(names)s group by `{field}`",
		{"names": names},
	)
	return {name: str(day) for name, day in rows}


@frappe.whitelist()
@frappe.read_only()
def keys(filters: dict | str | None = None) -> list[dict]:
	"""The keys of the window as `api.keys.key_rows` lists them, plus their team, model groups,
	the window's usage, the last day served and how much of the cap the month took. Every key
	of the filtered team; else the window's costliest, KEY_LIMIT of them. Live keys first."""
	operator()
	window = Window(filters)
	table, params = window.table()
	usage = {row["name"]: row for row in grouped(table, params, "x.api_key", KEY_LIMIT)}
	names = set(usage)
	if window.filters["team"]:
		names |= set(frappe.get_list("Grove API Key", filters={"team": window.filters["team"]}, pluck="name"))
	held = window.filters["api_key"]
	if isinstance(held, str):
		names.add(held)
	elif held is not None:
		names &= set(held)
	names.discard("")
	if not names:
		return []
	names = list(names)
	teams = dict(
		frappe.get_list(
			"Grove API Key", filters={"name": ("in", names)}, fields=["name", "team"], as_list=True
		)
	)
	groups, used = model_groups_of(names), last_used("api_key", names)
	rows = []
	for row in key_rows(names):
		row.pop("key_hash", None)
		cap = flt(row.cap)
		rows.append(
			{
				**row,
				"team": teams.get(row.name),
				"model_groups": groups.get(row.name, []),
				"cap_pct": round(row.month_spent / cap * 100, 1) if cap else None,
				"last_used": used.get(row.name),
				**usage_of(usage.get(row.name)),
			}
		)
	rows.sort(key=lambda row: (row["status"] != "active", -row["cost"]))
	return rows


def model_groups_of(names):
	rows = frappe.get_list(
		"Model Group Row",
		filters={"parenttype": "Grove API Key", "parent": ("in", names)},
		fields=["parent", "model_group"],
		parent_doctype="Grove API Key",
	)
	groups = {}
	for row in rows:
		groups.setdefault(row.parent, []).append(row.model_group)
	return groups


@frappe.whitelist()
@frappe.read_only()
def gateways() -> dict:
	"""Every gateway with its row of the last push and of the last usage pull, and the pause flags."""
	operator()
	settings = frappe.get_cached_doc("Grove Settings")
	pushes, pulls = rows_by_server("Projection"), rows_by_server("Usage")
	boxes = frappe.get_list(
		"Gateway Server",
		filters={"status": ("!=", "Terminated")},
		fields=[
			"name",
			"status",
			"geography",
			"gateway_store",
			"is_store_writer",
			"is_in_maintenance",
			"agent_version",
		],
		order_by="name",
	)
	for box in boxes:
		box["push"], box["pull"] = pushes.get(box.name), pulls.get(box.name)
	return {
		"gateways": boxes,
		"paused": {
			"projection": bool(settings.pause_projection),
			"usage_pull": bool(settings.pause_usage_pull),
		},
	}


def rows_by_server(sync_type):
	"""{server: its row of the newest Pathway Sync of `sync_type`, stamped with the run's time}."""
	run = last_sync(sync_type, with_rows=True)
	return {row.server: {**row, "run_at": run.get("run_at")} for row in run.get("rows", [])}


def last_sync(sync_type, with_rows=False):
	"""The newest Pathway Sync of `sync_type`, its rows when asked; the type alone when none ran."""
	runs = frappe.get_list(
		"Pathway Sync",
		filters={"sync_type": sync_type},
		fields=["name", "run_at", "status", "trigger", "targets_ok", "targets_total"],
		order_by="run_at desc",
		limit=1,
	)
	if not runs:
		return {"sync_type": sync_type}
	run = runs[0]
	if with_rows:
		run["rows"] = frappe.get_list(
			"Pathway Sync Row",
			filters={"parent": run.name},
			fields=[
				"server_type",
				"server",
				"reachable",
				"success",
				"had_data",
				"duration_ms",
				"detail",
				"error",
			],
			parent_doctype="Pathway Sync",
			order_by="idx",
		)
	return {"sync_type": sync_type, **run}


@frappe.whitelist()
@frappe.read_only()
def key_stats(filters: dict | str | None = None) -> dict:
	"""Each vendor key's counters summed over the Provider Key Stats rows pulled in the window (a
	row holds the six hours before its pull), busiest first; `provider` and `geography` narrow.
	`last_pull` is when a row last landed at all, for a window that holds none."""
	operator()
	given = frappe._dict(frappe.parse_json(filters) or {})
	window = Window(filters)
	sums = ", ".join(
		f"sum(cast(json_value(s.stats, '$.{count}') as unsigned)) as {count}" for count in COUNTS
	)
	lasts = ", ".join(f"max(json_value(s.stats, '$.{field}')) as {field}" for field in LAST)
	conditions = ["s.creation >= %(since)s", "s.creation < %(until)s"]
	# `creation` is site time; the window's UTC days become site-time bounds.
	params = {
		"since": site_datetime(f"{window.from_date}T00:00:00Z"),
		"until": site_datetime(f"{add_days(window.to_date, 1)}T00:00:00Z"),
	}
	for field in ("provider_name", "geography"):
		if value := given.get(field.removesuffix("_name")):
			conditions.append(f"p.`{field}` = %({field})s")
			params[field] = value
	rows = frappe.db.sql(
		f"""select s.model_provider, p.display_name as provider, p.provider_name, p.geography,
		s.key_title, s.key_id, max(mk.disabled) as disabled, {sums}, {lasts}
		from `tabProvider Key Stats` s
		join `tabModel Provider` p on p.name = s.model_provider
		left join `tabModel Provider Key` mk on mk.name = s.key_id
		where {" and ".join(conditions)}
		group by s.model_provider, p.display_name, p.provider_name, p.geography, s.key_title, s.key_id
		order by requests desc""",
		params,
		as_dict=True,
	)
	last_pull = frappe.db.sql("select max(creation) from `tabProvider Key Stats`")[0][0]
	return {
		"rows": [{**row, **{count: cint(row[count]) for count in COUNTS}} for row in rows],
		"last_pull": str(last_pull) if last_pull else None,
	}


# ponytail: every team and model in one answer; a search box past a few hundred of either.
OPTION_LIMIT = 500


@frappe.whitelist()
@frappe.read_only()
def filter_options(team: str | None = None) -> dict:
	"""What the filter row offers: the teams, geographies, models and vendor names, and the keys
	of `team` once one is picked (a key filter without a team is every key, too many to list)."""
	operator()
	keys = (
		frappe.get_list(
			"Grove API Key",
			filters={"team": team},
			fields=["name", "title", "masked", "status"],
			order_by="creation desc",
			limit=OPTION_LIMIT,
		)
		if team
		else []
	)
	return {
		"teams": frappe.get_list("Grove Team", pluck="name", order_by="name", limit=OPTION_LIMIT),
		"geographies": frappe.get_list("Geography", fields=["name", "label"], order_by="name"),
		"models": frappe.get_list(
			"Model", fields=["name", "model_key", "geography"], order_by="model_key", limit=OPTION_LIMIT
		),
		"providers": frappe.get_list(
			"Model Provider", pluck="provider_name", distinct=True, order_by="provider_name"
		),
		"keys": keys,
	}


@frappe.whitelist()
def peek(team: str) -> dict:
	"""On demand: what the boxes count this month for each of `team`'s keys beside what Grove's
	records hold, with a verdict on the gap. Refused as `peek_team` refuses: pushes paused, or a
	store unreachable."""
	operator()
	from grove.pathway.usage import peek_team

	keys = frappe.get_list(
		"Grove API Key", filters={"team": team}, fields=["name", "title", "status"], order_by="creation desc"
	)
	if not keys:
		frappe.throw(f"{team} has no keys.")
	window = frappe.db.get_value("Grove Team", team, "key_cap_window") or "Monthly"
	box = peek_team(team)
	grove = month_spent_by("api_key", [key.name for key in keys])
	return {
		"team": team,
		"key_cap_window": window,
		"rows": [drift_row(key, flt(grove.get(key.name, 0), 9), box.get(key.name), window) for key in keys],
	}


def drift_row(key, grove, gateway, window):
	row = {"key": key.name, "title": key.title, "status": key.status, "grove": grove, "gateway": None}
	if gateway is None:
		row["verdict"] = "no store holds this key"
		return row
	row["gateway"] = flt(gateway, 9)
	if window == "Total":
		row["verdict"] = "Total window: the box counts since the mint, Grove's figure is this month's"
		return row
	row["delta"] = round(row["gateway"] - grove, 9)
	if abs(row["delta"]) < 1e-9:
		row["verdict"] = "in step"
	elif row["delta"] < 0:
		row["verdict"] = "box behind Grove: its store was restored or flushed"
	else:
		row["verdict"] = "usage the next pull lands, or a drain lost on the box"
	return row
