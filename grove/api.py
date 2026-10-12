# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Provisioning API. The only user-facing surface: register a team, set its limits, mint its keys."""

import hmac

import frappe
from frappe.utils import flt

from grove.access import team_limits
from grove.utils import site_datetime, utc_today

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]
USAGE_FIELDS = ("requests", "tokens", "cost")
PULLS_PER_HOUR = 2
KEY_FIELDS = ["name", "title", "status", "creation", "key_hash", "geography", "cap", "spent"]

# NOTE: a Central Team is named by its id in Central (its doc name); Grove keeps nothing else about who it is.
# TODO: give machine info (like no of them, their type, etc) to central so they can find the unit economics


@frappe.whitelist()
def provision_team(team: str, free: bool = False):
	"""Register `team` — Central's own id for it, never a login. `free` ignores pricing
	for the team — otherwise every key spends up to its monthly cap, within the spend limit of
	the team's tier (`update_team` names it, and sets `free` too). Safe to repeat: a known team keeps whatever
	else is not given. → the team and how many live keys it may hold (0: no limit)."""
	frappe.only_for(ALLOWED_ROLES)
	if not team:
		frappe.throw("A team needs an id.")
	if frappe.db.exists("Central Team", team):
		doc = frappe.get_doc("Central Team", team)
	else:
		doc = frappe.get_doc({"doctype": "Central Team", "__newname": team})
	if free:
		doc.free = 1
	doc.save()
	return {"team": doc.name, "max_keys": team_limits(doc.name).max_keys}


@frappe.whitelist()
def update_team(team: str, suspended: bool | None = None, free: bool | None = None, tier: str | None = None):
	"""Which Tier `team` runs under and whether it is in good standing, each kept when not given:
	`tier`, named after Central's trust tier — its key count, monthly spend limit and rate limits
	are Grove's to set ("" clears it, the default tier applies; a name Grove has not seen is
	created as a copy of the default tier, for the operator to tune); `free`, never charged or
	gated, and turning it off resets every live key's cap to 0; and `suspended`, which refuses
	every key of the team from the next sync. Lowering a limit stops no key: it refuses a new key
	or a raise until the team fits again. → the team and the limits in force for it."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.tier.tier import ensure

	doc = frappe.get_doc("Central Team", get_team(team))
	if tier:
		ensure(tier)
	for field, value in (("free", free), ("suspended", suspended), ("tier", tier)):
		if value is not None:
			doc.set(field, value)
	doc.save()
	return {"team": doc.name, **team_limits(doc.name)}


@frappe.whitelist()
def provision_key(
	team: str, title: str | None = None, geography: str | None = None, cap: float | None = None,
	models: list[str] | None = None, limits: list[dict] | None = None,
):
	"""Mint a key for `team` in `geography` — the default one when none is given; every other
	geography's gateway refuses it. `title` labels the key, to tell a team's keys apart. `cap` is
	what it may spend a month, refused when the team's caps would pass its spend limit; left out,
	the key is minted at 0 and its gateway refuses it until `update_key(cap=)` gives it one. A
	Free team's caps are not checked. The key starts in its geography's default Model Group;
	`models` are its own on top. `limits` pin its rate limits, rows of `metric` (requests,
	total_tokens), `window` (1m, 1h, 1d, 1M) and `value`; given none it runs under its team's
	Tier (`update_team`), else the default tier's, else 20 requests and 100 000 tokens a minute. →
	the key's name, its geography and the endpoint it calls, and the secret — shown here and
	never again."""
	frappe.only_for(ALLOWED_ROLES)
	key = frappe.new_doc("Grove API Key")
	key.team, key.title, key.status = get_team(team), title, "active"
	if geography:
		key.geography = geography
	set_key_policy(key, cap, models, limits)
	key.insert()
	gateway_url = get_gateway_url(key.geography)
	if not gateway_url:
		frappe.throw(f"{key.geography!r} has no endpoint.")
	return {
		"name": key.name,
		"geography": key.geography,
		"gateway_url": gateway_url,
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def update_key(team: str, key: str, cap: float | None = None, models: list[str] | None = None, limits: list[dict] | None = None):
	"""Change what a key of `team` may spend or call: `cap`, `models` (replacing its own Allow)
	and `limits` (replacing its own rate limits; `[]` puts it back under its team's tier), each
	kept when not given. Its geography never changes — mint another key for that. → the key as
	`keys` lists it."""
	frappe.only_for(ALLOWED_ROLES)
	doc = get_api_key(team, key)
	set_key_policy(doc, cap, models, limits)
	doc.save()
	return key_rows([doc.name])[0]


@frappe.whitelist()
def update_caps(team: str, caps: dict[str, float]):
	"""Set several monthly caps of `team`'s live keys at once, {key: USD}. Lowered caps are saved
	first, so moving a cap from one key to another fits the spend limit midway as well; any
	refusal leaves every cap as it was. → the keys as `keys` lists them."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.grove_api_key.grove_api_key import capped

	team = get_team(team)
	docs = [get_api_key(team, key) for key in caps]
	if revoked := [doc.name for doc in docs if doc.status != "active"]:
		frappe.throw(f"{', '.join(revoked)} is revoked.")
	team_doc = team_limits(team, for_update=True)
	total = flt(capped(team) + sum(flt(caps[doc.name]) - flt(doc.cap) for doc in docs), 9)
	# Lowers go first, so the total only climbs to this: checked once, before anything is saved.
	raised = any(flt(caps[doc.name]) > flt(doc.cap) for doc in docs)
	if not team_doc.free and raised and total > flt(team_doc.spend_limit, 9):
		frappe.throw(f"Caps would add up to {total:.2f} a month of a spend limit of {flt(team_doc.spend_limit):.2f}.")
	for doc in sorted(docs, key=lambda doc: flt(caps[doc.name]) - flt(doc.cap)):
		doc.cap = flt(caps[doc.name])
		doc.save()
	return key_rows([doc.name for doc in docs])


@frappe.whitelist()
def keys(team: str):
	"""The keys of `team`, newest first, never with their secret: that is shown once, by
	`provision_key`. Each with its geography and the endpoint it calls, its monthly cap and what
	it has spent this month (`month_spent`, off the records) and in all (`spent`), its rate limits
	as the gateway counts them (its own, else its team's tier's, else the defaults), `key_hash`
	(narrows `usage` to it) and `revocable_at` (UTC): when `revoke_key` stops refusing a new key,
	so a caller need not ask before then. Nothing for a team Grove does not know."""
	frappe.only_for(ALLOWED_ROLES)
	return key_rows(frappe.get_list("Grove API Key", filters={"team": team or ""}, order_by="creation desc", pluck="name"))


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
def limits(team: str, key: str):
	"""The rate limits of one key of `team`, rows of `metric`, `window` and `value`, as the gateway
	counts them for that key alone: its own rows, else its team's tier's, else the defaults.
	Refused for a key the team does not hold."""
	frappe.only_for(ALLOWED_ROLES)
	return limit_rows_of([get_api_key(team, key).name]).get(key, [])


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
def revoke_key(team: str, key: str):
	"""Revoke a key of `team` by the name `keys` lists it under. The row stays as the record it
	existed, its cap leaves room under the team's spend limit, and the next sync prunes it from
	every proxy."""
	frappe.only_for(ALLOWED_ROLES)
	get_api_key(team, key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist()
def geographies():
	"""Where a key may be minted: every Geography, its endpoint, and which one a key gets when
	none is asked for."""
	frappe.only_for(ALLOWED_ROLES)
	rows = frappe.get_list("Geography", fields=["name", "label", "endpoint", "is_default"], order_by="name asc")
	return [
		{"name": r.name, "label": r.label or r.name, "endpoint": f"https://{r.endpoint}", "is_default": bool(r.is_default)}
		for r in rows
	]


@frappe.whitelist(allow_guest=True, methods=["POST"])
def create_control_client(email: str):
	# the secret should not be stored in the Request Log.
	token = frappe.form_dict.pop("token", None)
	expected = frappe.conf.get("control_secret")

	if not (expected and token) or not hmac.compare_digest(str(token), str(expected)):
		frappe.throw("Invalid Operation", frappe.AuthenticationError)

	control_user = _create_control_user(email)

	# Mint directly: generate_keys() would reject a Guest caller on permission.
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}


@frappe.whitelist()
def create_control_client_key():
	frappe.only_for(ALLOWED_ROLES)

	control_user = frappe.get_doc("User", frappe.session.user)
	api_secret = frappe.generate_hash(length=15)
	control_user.api_key = control_user.api_key or frappe.generate_hash(length=15)
	control_user.api_secret = api_secret
	control_user.save(ignore_permissions=True)

	return {"api_key": control_user.api_key, "api_secret": api_secret, "user": control_user.name}


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


@frappe.whitelist()
def available_models(team: str | None = None, key: str | None = None, geography: str | None = None):
	"""What a new key in `geography` (the default one when none is given) starts with: the
	published models of that geography's default Model Group, one row per key. `name` is the key
	a caller sends (`provider`/`model_id`); `provider` is the provider's name. With `team` and
	`key`, what that one key may call, as its own geography serves it. `dialects` names the
	surfaces (openai, anthropic) each one answers on there. The gateway is what enforces access;
	this is for showing a person."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.access import get_reachable_models, model_rows
	from grove.grove.doctype.model.model import get_modalities
	from grove.pathway.routes import get_dialects, models_in

	geography = geography or frappe.db.get_value("Geography", {"is_default": 1})
	if key:
		api_key = get_api_key(team, key)
		geography, reachable = api_key.geography, set(get_reachable_models(api_key.name))
	else:
		default = frappe.db.get_value("Model Group", {"is_default": 1, "geography": geography})
		reachable = set(model_rows("Model Group", [default]).get(default, {}).get("models", [])) if default else set()
	if not reachable:
		return []
	models = [m for m in models_in(geography) if m.published and m.model_key in reachable]
	dialects = get_dialects(models, geography)
	modalities = get_modalities([m.name for m in models])
	return [
		{
			"name": m.model_key, "model_id": m.model_id, **modalities[m.name],
			"provider": m.provider_name, "geography": m.geography, "dialects": dialects[m.name],
		}
		for m in sorted(models, key=lambda m: m.model_key)
	]


def get_team(team):
	"""`team`, Central's id, once it is known here; an unknown one is refused."""
	if not team or not frappe.db.exists("Central Team", team):
		frappe.throw(f"No Central Team {team!r}.")
	return team


def get_gateway_url(geography):
	"""Where a key of `geography` works: its endpoint, None while it has none."""
	host = frappe.db.get_value("Geography", geography, "endpoint")
	return f"https://{host}" if host else None


def get_api_key(team, key):
	"""The Grove API Key doc behind `key` when `team` holds it: another team's key name must not
	reach it. An unknown one is refused."""
	name = frappe.db.get_value("Grove API Key", {"name": key or "", "team": team or ""})
	if not name:
		frappe.throw("no such API key", frappe.DoesNotExistError)
	return frappe.get_doc("Grove API Key", name)


def set_key_policy(doc, cap=None, models=None, limits=None):
	"""Write onto `doc` what was given: `cap`; `models`, replacing its own Allow with that
	geography's docs; `limits`, replacing its rate limits."""
	if cap is not None:
		doc.cap = cap
	if models is not None:
		from grove.access import model_doc

		doc.allow = []
		for model in models:
			doc.append("allow", {"model": model_doc(model, doc.geography or frappe.db.get_value("Geography", {"is_default": 1}))})
	if limits is not None:
		doc.set("limits", limits)


def key_rows(names):
	"""The keys named, as `keys` lists them: KEY_FIELDS plus the endpoint, the limits, the
	masked secret, `month_spent` and `revocable_at`. A handful of queries however many keys."""
	from frappe.utils.password import get_decrypted_password

	from grove.billing.pricing import month_spent_by
	from grove.grove.doctype.grove_api_key.grove_api_key import KEY_PREFIX, REVOKE_AFTER_HOURS
	from grove.pathway.routes import utc_timestamp

	if not names:
		return []
	rows = frappe.get_list("Grove API Key", filters={"name": ("in", names)}, fields=KEY_FIELDS, order_by="creation desc")
	limits = limit_rows_of(names)
	month = month_spent_by("api_key", names)
	for row in rows:
		secret = get_decrypted_password("Grove API Key", row.name, "api_secret")
		# The prefix and four characters: enough to tell keys apart, nothing of the end.
		row["masked"] = f"{secret[:len(KEY_PREFIX) + 4]}…"
		row["gateway_url"] = get_gateway_url(row.geography)
		row["limits"] = limits.get(row.name, [])
		row["revocable_at"] = utc_timestamp(frappe.utils.add_to_date(row.creation, hours=REVOKE_AFTER_HOURS))
		row["month_spent"] = flt(month.get(row.name, 0), 9)
	return rows


def limit_rows_of(names):
	"""{key: [{metric, window, value}, ...]} for these keys as the gateway counts them: their own
	rows, else their team's Tier's, else the default tier's, else the defaults."""
	from grove.access import effective_limits, limit_entries

	keys = frappe.get_list("Grove API Key", filters={"name": ("in", names)}, fields=["name", "team"])
	tiers = frappe.get_list("Central Team", filters={"name": ("in", [k.team for k in keys] or [""])}, fields=["name", "tier"])
	return effective_limits(keys, limit_entries("Grove API Key", names), {t.name: t.tier for t in tiers})


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


def _create_control_user(email):
	if frappe.db.exists("User", email):
		frappe.throw("Invalid Operation")

	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = "Control Client"
	doc.user_type = "Website User"
	doc.send_welcome_email = 0
	doc.enabled = 1
	doc.append("roles", {"role": CONTROL_ROLE})
	doc.insert(ignore_permissions=True)
	return doc


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
