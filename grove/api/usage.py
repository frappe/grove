# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt

from grove.access import team_limits
from grove.api.control import ALLOWED_ROLES
from grove.api.teams import get_team
from grove.utils import site_datetime, utc_today

USAGE_FIELDS = ("requests", "tokens", "cost")
PULLS_PER_HOUR = 2

# TODO: give machine info (like no of them, their type, etc) to central so they can find the unit economics


@frappe.whitelist()
def spend(team: str):
	"""What `team` has spent and may spend, as of the last pull (up to an hour behind the
	gateways; `pull_usage` first for a fresh figure): `spent` in all and `month_spent` this UTC
	month (the billed records of every key it held, revoked ones too, plus what Grove corrected);
	`capped`, Σ its live keys' monthly caps, against `spend_limit`; `max_keys` (0: no limit),
	`suspended`. A free team is never charged and never gated: its `spent` does not move.
	Credit and invoices are Central's."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.pricing import month_spent_by
	from grove.grove.doctype.grove_api_key.grove_api_key import capped

	team = get_team(team)
	doc = team_limits(team)
	doc.spent = frappe.db.get_value("Central Team", team, "spent")
	return {
		"spent": flt(doc.spent, 9),
		"month_spent": flt(month_spent_by("team", [team]).get(team, 0), 9),
		"capped": capped(team),
		"spend_limit": flt(doc.spend_limit, 9),
		"max_keys": doc.max_keys,
		"suspended": bool(doc.suspended),
	}


@frappe.whitelist()
def pull_usage(team: str):
	"""Drain the keys of `team` from every store now, waiting for a pull in flight — for a fresh
	`spend`. PULLS_PER_HOUR per team. → the Pathway Sync that logged it, or None when there was
	nothing to drain."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.pathway.usage import pull_all

	# TODO: this needs to trigger it but not sync it in the web thread
	_count_pull(get_team(team))
	return {"sync": pull_all(trigger="Manual", wait=60, team=team)}


@frappe.whitelist()
@frappe.read_only()
def usage(
	teams: list[str] | str, from_date: str | None = None, to_date: str | None = None,
	period: str | None = None, month: str | None = None, key_hash: str | None = None,
	pulled_after: str | None = None, pulled_until: str | None = None,
):
	"""Requests and cost per team and per model over a UTC date range: `from_date`/`to_date`, a
	`period` (Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month) or a `month`
	(YYYY-MM); This Month when none is given. Or over a landing window instead: `pulled_after` /
	`pulled_until` (RFC 3339 UTC, until exclusive) select what was pulled in that window whatever
	day it was served on, so an invoice is every record landed since the last one — a drain that
	lands late goes on the next, and nothing is billed twice. `teams` are Central's ids, and each
	one's totals come back under it. Summed by the database in one grouped query. `tokens` are
	the whole prompt and completion tokens; `cost` is what was charged, so usage while the team
	was Free adds requests and tokens and no cost, and a Credit Discrepancy Grove corrected adds
	its correction under its model as of when it was resolved. `daily_summary` is the per-model
	summary again, per UTC day, for a chart. `as_of` is when the newest usage in the range was
	pulled, in UTC. `key_hash` narrows it all to one key of theirs. Read-only: on a site with
	`read_from_replica` it runs against the replica, which the hourly pull makes safe — nothing
	here is read back right after a write."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.doctype.usage_record.usage_record import money_rows
	from grove.pathway.routes import utc_timestamp

	if isinstance(teams, str):
		teams = [teams]
	window = read_window(from_date, to_date, period, month, pulled_after, pulled_until)
	known = frappe.get_list("Central Team", {"name": ("in", teams)}, pluck="name")
	api_key = _key_of(key_hash, known) if key_hash else None
	table, params = money_rows({"team": known, "api_key": api_key}, **window)
	rows = frappe.db.sql(
		f"""select team, model, sum(requests) as requests, sum(tokens) as tokens, sum(cost) as cost,
		max(pulled_at) as as_of from {table} group by team, model""",
		params,
		as_dict=True,
	) if known else []
	per_model = [
		{
			"model": row.model, "team": row.team, "requests": int(row.requests or 0),
			"tokens": int(row.tokens or 0), "cost": float(row.cost or 0),
		}
		for row in rows
	]
	totals = {team: {"requests": 0, "tokens": 0, "cost": 0.0} for team in known}
	for row in per_model:
		for field in USAGE_FIELDS:
			totals[row["team"]][field] += row[field]
	return {
		"teams": teams,
		"from_date": str(window["from_date"]) if "from_date" in window else None,
		"to_date": str(window["to_date"]) if "to_date" in window else None,
		"as_of": utc_timestamp(max(row.as_of for row in rows)) if rows else None,
		"model_summary": _totals_by_model(per_model),
		"daily_summary": _daily_summary(table, params) if known else [],
		**totals,
	}


def _count_pull(team):
	"""Refuse past PULLS_PER_HOUR on-demand pulls of one team. The hour starts at the first."""
	key = frappe.cache.make_key(f"usage_pull:{team}")
	count = frappe.cache.incr(key)
	# Checked every call, not only on the first: a counter left without a TTL would block forever.
	if frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, 60 * 60)
	if count > PULLS_PER_HOUR:
		frappe.throw(f"{PULLS_PER_HOUR} usage pulls an hour per team.", frappe.RateLimitExceededError)


def read_window(from_date, to_date, period, month, pulled_after, pulled_until):
	"""What a read covers, as `money_rows` takes it: the UTC days asked for, or the landing window
	`pulled_after`..`pulled_until` (RFC 3339 UTC) as site-time datetimes."""
	if pulled_after or pulled_until:
		if not (pulled_after and pulled_until):
			frappe.throw("A landing window needs both pulled_after and pulled_until.")
		return {"since": site_datetime(pulled_after), "until": site_datetime(pulled_until)}
	first, last = usage_window(from_date, to_date, period, month)
	return {"from_date": first, "to_date": last}


def usage_window(from_date, to_date, period, month):
	"""(first day, last day) in UTC for whichever way the range was asked."""
	from frappe.utils import add_days, add_months, get_first_day, get_last_day, getdate

	today = utc_today()
	if from_date or to_date:
		return getdate(from_date or to_date), getdate(to_date or from_date)
	if month:
		first = get_first_day(f"{month}-01")
		return first, get_last_day(first)
	last_month = add_months(get_first_day(today), -1)
	windows = {
		"Today": (today, today),
		"Yesterday": (add_days(today, -1), add_days(today, -1)),
		"Last 7 Days": (add_days(today, -6), today),
		"Last 30 Days": (add_days(today, -29), today),
		"This Month": (get_first_day(today), today),
		"Last Month": (last_month, get_last_day(last_month)),
	}
	if (period or "This Month") not in windows:
		frappe.throw(f"Unknown period {period!r}: one of {', '.join(windows)}.")
	return windows[period or "This Month"]


def _totals_by_model(rows):
	"""Usage rows folded into one entry per model, costliest first, then busiest. Rows arrive one
	per (team, model), so a model is summed across teams rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(
			row["model"], {"model": row["model"], **dict.fromkeys(USAGE_FIELDS, 0)}
		)
		for f in USAGE_FIELDS:
			totals[f] += row.get(f) or 0

	return sorted(per_model.values(), key=lambda totals: (-totals["cost"], -totals["requests"]))


def _key_of(key_hash, teams):
	"""The key behind `key_hash`, only when one of these teams holds it: another team's key must
	not reveal its usage."""
	name = frappe.db.get_value(
		"Grove API Key", {"key_hash": key_hash, "team": ("in", teams or [""])}
	)
	if not name:
		frappe.throw("No such API key for these teams.", frappe.DoesNotExistError)
	return name


def _daily_summary(table, params):
	"""Requests, tokens and cost per UTC day and model over the same `money_rows` table `usage`
	summed, oldest day first. A day with no usage has no entry."""
	rows = frappe.db.sql(
		f"""select day, model, sum(requests) as requests, sum(tokens) as tokens, sum(cost) as cost
		from {table} group by day, model order by day, model""",
		params,
		as_dict=True,
	)
	return [
		{
			"day": str(row.day), "model": row.model, "requests": int(row.requests or 0),
			"tokens": int(row.tokens or 0), "cost": float(row.cost or 0),
		}
		for row in rows
	]
