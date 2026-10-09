# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Provisioning API. The only user-facing surface: register a team, mint its keys, fund it."""

import hmac

import frappe
from frappe.utils import flt

from grove.utils import utc_today

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]
USAGE_FIELDS = ("requests", "tokens", "cost")
PULLS_PER_HOUR = 2
KEY_FIELDS = ["name", "title", "status", "creation", "key_hash", "geography", "cap", "spent"]

# NOTE: a Central Team is named by its id in Central (its doc name); its email is the team owner's (TODO: this should be notif email).
# TODO: give machine info (like no of them, their type, etc) to central so they can find the unit economics


@frappe.whitelist()
def provision_team(team: str, email: str, free: bool = False):
	"""Register `team` — Central's own id for it, never a login. `email` is where its alerts go;
	it is written on every call, and one address may sit on several teams. `free` ignores pricing
	for the team — otherwise it is prepaid and every key spends against a cap cut from its balance.
	Safe to repeat: a known team keeps whatever else is not given. → the team and how many live
	keys it may hold."""
	frappe.only_for(ALLOWED_ROLES)
	if not team:
		frappe.throw("A team needs an id.")
	if frappe.db.exists("Central Team", team):
		doc = frappe.get_doc("Central Team", team)
	else:
		doc = frappe.get_doc({"doctype": "Central Team", "__newname": team})
	doc.email = email
	if free:
		doc.free = 1
	doc.save()
	return {"team": doc.name, "max_keys": doc.max_keys}


@frappe.whitelist()
def provision_key(
	team: str, title: str | None = None, geography: str | None = None, cap: float | None = None,
	models: list[str] | None = None, limits: list[dict] | None = None,
):
	"""Mint a key for `team` in `geography` — the default one when none is given; every other
	geography's gateway refuses it. `title` labels the key, to tell a team's keys apart. `cap` is
	what it may spend out of the team's balance, refused past what no other key's cap has
	claimed; left out on a prepaid team, the key is minted at 0 and its gateway refuses it until
	`update_key(cap=)` or a top-up's allocation gives it one. A Free team's keys have none. The key
	starts in its geography's default Model Group; `models` are its own on
	top. `limits` are its rate limits, rows of `metric` (requests, total_tokens), `window` (1m,
	1h, 1d, 1M) and `value`; given none it starts under the defaults, 20 requests and 100 000
	tokens a minute. → the key's name, its geography and the endpoint it calls, and the secret —
	shown here and never again."""
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
	and `limits` (replacing its rate limits), each kept when not given. Its geography never
	changes — mint another key for that. → the key as `keys` lists it."""
	frappe.only_for(ALLOWED_ROLES)
	doc = get_api_key(team, key)
	set_key_policy(doc, cap, models, limits)
	doc.save()
	return key_rows([doc.name])[0]


@frappe.whitelist()
def keys(team: str):
	"""The keys of `team`, newest first, never with their secret: that is shown once, by
	`provision_key`. Each with its geography and the endpoint it calls, its cap and what it has
	spent, its rate limits, `key_hash` (narrows `usage` to it) and `revocable_at` (UTC): when
	`revoke_key` stops refusing a new key, so a caller need not ask before then. Nothing for a
	team Grove does not know."""
	frappe.only_for(ALLOWED_ROLES)
	return key_rows(frappe.get_list("Grove API Key", filters={"team": team or ""}, order_by="creation desc", pluck="name"))


@frappe.whitelist()
def add_credit(
	team: str, amount: float, note: str | None = None, reference: str | None = None,
	allocations: dict[str, float] | None = None,
):
	"""Append one ledger entry for `team` and settle it. 0 and an unexplained negative are
	refused by the ledger itself. `reference` is the caller's own id for the top-up: a call that
	repeats one adds nothing, so a call that timed out is safe to send again. `allocations`,
	{key: USD}, says how much of it each key may spend; none spreads a prepaid team's top-up over
	its live keys in proportion to their caps. → the balance after, and the `allocations` made."""
	frappe.only_for(ALLOWED_ROLES)
	get_team(team)
	repeated = reference and frappe.db.get_value(
		"Grove Credit", {"reference": reference}, ["name", "team", "amount"], as_dict=True
	)
	if not repeated:
		rows = [{"api_key": key, "amount": value} for key, value in (allocations or {}).items()]
		entry = frappe.get_doc({
			"doctype": "Grove Credit", "team": team, "amount": amount, "note": note, "reference": reference,
			"allocations": rows,
		}).insert()
	# A repeat has to be the same top-up: one id on another team or amount is a caller bug.
	elif (repeated.team, flt(repeated.amount, 9)) != (team, flt(amount, 9)):
		frappe.throw(f"Reference {reference!r} already names another top-up.")
	else:
		entry = frappe.get_doc("Grove Credit", repeated.name)
	# TODO: we should send a message like it might take some time to reflect
	return {
		"balance": frappe.db.get_value("Central Team", team, "balance"),
		"allocations": [{"key": row.api_key, "amount": row.amount} for row in entry.allocations],
	}


@frappe.whitelist()
def balance(team: str):
	"""What `team` has left: Σ ledger less usage priced so far, as of the last pull (up to an hour
	behind the gateways; `pull_usage` first for a fresh figure), and `unallocated`: the part no
	live key's cap has claimed yet. A free team is never charged and never gated: its `spent`
	does not move."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.pricing import credit_summary

	summary = credit_summary(get_team(team))
	return {
		"balance": float(summary["remaining"]),
		"spent": float(summary["spent"]),
		"unallocated": float(summary["unallocated"]),
		"is_free_user": bool(frappe.db.get_value("Central Team", team, "free")),
	}


@frappe.whitelist()
def limits(team: str, key: str):
	"""The rate limits of one key of `team`, rows of `metric`, `window` and `value`, as the gateway
	counts them for that key alone. Refused for a key the team does not hold."""
	frappe.only_for(ALLOWED_ROLES)
	return limit_rows_of([get_api_key(team, key).name]).get(key, [])


@frappe.whitelist()
def pull_usage(team: str):
	"""Drain the keys of `team` from every store now, waiting for a pull in flight — for a fresh
	`balance`. PULLS_PER_HOUR per team. → the Pathway Sync that logged it, or None when there was
	nothing to drain."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.pathway.usage import pull_all

	# TODO: this needs to trigger it but not sync it in the web thread
	_count_pull(get_team(team))
	return {"sync": pull_all(trigger="Manual", wait=60, team=team)}


@frappe.whitelist()
def revoke_key(team: str, key: str):
	"""Revoke a key of `team` by the name `keys` lists it under. The row stays as the record it
	existed, its unspent cap returns to the team, and the next sync prunes it from every proxy."""
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
):
	"""Requests and cost per team and per model over a UTC date range: `from_date`/`to_date`, a
	`period` (Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month) or a `month`
	(YYYY-MM); This Month when none is given. `teams` are Central's ids, and each one's totals
	come back under it. Summed by the database in one grouped query. `tokens` are the
	whole prompt and completion tokens; `cost` is what was charged, so usage while the team was
	Free adds requests and tokens and no cost.
	`daily_summary` is the per-model summary again, per UTC day, for a chart. `as_of` is when the
	newest usage in the range was pulled, in UTC. `key_hash` narrows it all to one key of theirs.
	Read-only: on a site with `read_from_replica` it runs against the replica, which the hourly
	pull makes safe — nothing here is read back right after a write."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.doctype.usage_record.usage_record import tokens_expression, usage_table
	from grove.pathway.routes import utc_timestamp

	if isinstance(teams, str):
		teams = [teams]
	from_date, to_date = usage_window(from_date, to_date, period, month)
	known = frappe.get_list("Central Team", {"name": ("in", teams)}, pluck="name")
	api_key = _key_of(key_hash, known) if key_hash else None
	rows = frappe.db.sql(
		f"""select r.team, u.model, sum(u.requests) as requests, sum({tokens_expression()}) as tokens,
		sum(if(r.billed, u.grove_cost, 0)) as cost, max(r.creation) as as_of
		from `tabUsage Record` r, {usage_table()}
		where r.team in %(teams)s and r.day between %(from_date)s and %(to_date)s {_key_condition(api_key)}
		group by r.team, u.model""",
		{"teams": known, "from_date": from_date, "to_date": to_date, "api_key": api_key},
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
		"teams": teams, "from_date": str(from_date), "to_date": str(to_date),
		"as_of": utc_timestamp(max(row.as_of for row in rows)) if rows else None,
		"model_summary": _totals_by_model(per_model),
		"daily_summary": _daily_summary(known, from_date, to_date, api_key) if known else [],
		**totals,
	}


@frappe.whitelist()
def available_models(team: str | None = None, key: str | None = None, geography: str | None = None):
	"""What a new key in `geography` (the default one when none is given) starts with: the
	published models of that geography's default Model Group, one row per key, `name` being
	the key a caller sends. With `team` and `key`, what that one key may call, as its own
	geography serves it. `dialects` names the surfaces (openai, anthropic) each one answers on
	there. The gateway is what enforces access; this is for showing a person."""
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
			"provider": m.provider, "geography": m.geography, "dialects": dialects[m.name],
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
	masked secret and `revocable_at`. Two queries however many keys."""
	from frappe.utils.password import get_decrypted_password

	from grove.grove.doctype.grove_api_key.grove_api_key import REVOKE_AFTER_HOURS
	from grove.pathway.routes import utc_timestamp

	if not names:
		return []
	rows = frappe.get_list("Grove API Key", filters={"name": ("in", names)}, fields=KEY_FIELDS, order_by="creation desc")
	limits = limit_rows_of(names)
	for row in rows:
		secret = get_decrypted_password("Grove API Key", row.name, "api_secret")
		row["masked"] = f"{secret[:6]}…{secret[-4:]}"
		row["gateway_url"] = get_gateway_url(row.geography)
		row["limits"] = limits.get(row.name, [])
		row["revocable_at"] = utc_timestamp(frappe.utils.add_to_date(row.creation, hours=REVOKE_AFTER_HOURS))
	return rows


def limit_rows_of(names):
	"""{key: [{metric, window, value}, ...]} for these keys, one query."""
	rows = frappe.get_list(
		"Model Limit", filters={"parent": ("in", names)}, fields=["parent", "metric", "window", "value"],
		parent_doctype="Grove API Key", order_by="metric asc, window asc",
	)
	grouped = {}
	for row in rows:
		grouped.setdefault(row.parent, []).append({"metric": row.metric, "window": row.window, "value": row.value})
	return grouped


def _count_pull(team):
	"""Refuse past PULLS_PER_HOUR on-demand pulls of one team. The hour starts at the first."""
	key = frappe.cache.make_key(f"usage_pull:{team}")
	count = frappe.cache.incr(key)
	# Checked every call, not only on the first: a counter left without a TTL would block forever.
	if frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, 60 * 60)
	if count > PULLS_PER_HOUR:
		frappe.throw(f"{PULLS_PER_HOUR} usage pulls an hour per team.", frappe.RateLimitExceededError)


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


def _key_condition(api_key):
	return "and r.api_key = %(api_key)s" if api_key else ""


def _daily_summary(teams, from_date, to_date, api_key=None):
	"""Requests, tokens and cost per UTC day and model across these teams, and one key of theirs
	when given, summed by the database, oldest day first. A day with no usage has no entry."""
	from grove.billing.doctype.usage_record.usage_record import tokens_expression, usage_table

	rows = frappe.db.sql(
		f"""select r.day, u.model, sum(u.requests) as requests, sum({tokens_expression()}) as tokens,
		sum(if(r.billed, u.grove_cost, 0)) as cost
		from `tabUsage Record` r, {usage_table()}
		where r.team in %(teams)s and r.day between %(from_date)s and %(to_date)s {_key_condition(api_key)}
		group by r.day, u.model order by r.day, u.model""",
		{"teams": teams, "from_date": from_date, "to_date": to_date, "api_key": api_key},
		as_dict=True,
	)
	return [
		{
			"day": str(row.day), "model": row.model, "requests": int(row.requests or 0),
			"tokens": int(row.tokens or 0), "cost": float(row.cost or 0),
		}
		for row in rows
	]
