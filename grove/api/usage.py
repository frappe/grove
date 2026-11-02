# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import flt

from grove.access import team_limits
from grove.api.control import ALLOWED_ROLES
from grove.api.keys import key_rows
from grove.api.teams import get_team
from grove.utils import site_datetime, utc_today

USAGE_FIELDS = ("requests", "tokens", "cost")
PULLS_PER_HOUR = 5


@frappe.whitelist()
@frappe.read_only()
def usage(
	teams: list[str] | str,
	from_date: str | None = None,
	to_date: str | None = None,
	period: str | None = None,
	month: str | None = None,
	key_hash: str | None = None,
	pulled_after: str | None = None,
	pulled_until: str | None = None,
):
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.doctype.usage_record.usage_record import money_rows
	from grove.pathway.routes import utc_timestamp

	if isinstance(teams, str):
		teams = [teams]
	window = read_window(from_date, to_date, period, month, pulled_after, pulled_until)
	known = frappe.get_list("Grove Team", {"name": ("in", teams)}, pluck="name")
	api_key = _key_of(key_hash, known) if key_hash else None
	table, params = money_rows({"team": known, "api_key": api_key}, **window)
	rows = (
		frappe.db.sql(
			f"""select team, model, sum(requests) as requests, sum(tokens) as tokens, sum(cost) as cost,
		max(pulled_at) as as_of from {table} group by team, model""",
			params,
			as_dict=True,
		)
		if known
		else []
	)
	per_model = [
		{
			"model": row.model,
			"team": row.team,
			"requests": int(row.requests or 0),
			"tokens": int(row.tokens or 0),
			"cost": float(row.cost or 0),
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


@frappe.whitelist()
def figures(team: str):
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.pricing import remembered_figures

	team = get_team(team)
	return remembered_figures(
		team,
		lambda: {
			"spend": spend_of(team),
			"keys": key_rows(
				frappe.get_list(
					"Grove API Key", filters={"team": team}, order_by="creation desc", pluck="name"
				)
			),
			"usage": usage([team], period="This Month"),
		},
	)


def spend_of(team):
	from grove.billing.pricing import committed, month_spent_by
	from grove.grove.doctype.grove_api_key.grove_api_key import capped

	doc = team_limits(team)
	return {
		"month_spent": flt(month_spent_by("team", [team]).get(team, 0), 9),
		"capped": capped(team),
		"committed": committed(team, lock=False),
		"spend_limit": None if doc.key_cap_window == "Total" else flt(doc.spend_limit, 9),
		"max_keys": doc.max_keys,
		"suspended": bool(doc.suspended),
		"log_payloads": bool(doc.log_payloads),
		"key_cap_window": doc.key_cap_window or "Monthly",
	}


@frappe.whitelist()
def pull_usage(team: str):
	frappe.only_for(ALLOWED_ROLES)
	from grove.pathway.usage import pull_all

	# TODO: this needs to trigger it but not sync it in the web thread
	_count_pull(get_team(team))
	sync = pull_all(trigger="Manual", wait=60, team=team)
	return {"sync": sync, "figures": figures(team)}


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
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(row["model"], {"model": row["model"], **dict.fromkeys(USAGE_FIELDS, 0)})
		for f in USAGE_FIELDS:
			totals[f] += row.get(f) or 0

	return sorted(per_model.values(), key=lambda totals: (-totals["cost"], -totals["requests"]))


def _key_of(key_hash, teams):
	name = frappe.db.get_value("Grove API Key", {"key_hash": key_hash, "team": ("in", teams or [""])})
	if not name:
		frappe.throw("No such API key for these teams.", frappe.DoesNotExistError)
	return name


def _daily_summary(table, params):
	rows = frappe.db.sql(
		f"""select day, model, sum(requests) as requests, sum(tokens) as tokens, sum(cost) as cost
		from {table} group by day, model order by day, model""",
		params,
		as_dict=True,
	)
	return [
		{
			"day": str(row.day),
			"model": row.model,
			"requests": int(row.requests or 0),
			"tokens": int(row.tokens or 0),
			"cost": float(row.cost or 0),
		}
		for row in rows
	]
