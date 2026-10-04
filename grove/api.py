# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Provisioning API. The only user-facing surface: register a user, mint an API key, etc."""

import hmac

import frappe
from frappe.utils import flt

from grove.grove.doctype.grove_user.grove_user import for_reference
from grove.utils import utc_today

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]
USAGE_FIELDS = ("requests", "cost")
PULLS_PER_HOUR = 3

# NOTE: a user in grove is a team in central, named by the team id; its email is the team owner's
# TODO: give machine info (like no of them, their type, etc) to central so they can find the unit economics


@frappe.whitelist()
def provision_user(
	user: str, email: str, geography: str = None, allowed_models: list[str] = None, free: bool = False,
	limits: list[dict] = None,
):
	"""Register `user` — the caller's own id for them, never a login — and pin them to
	`geography`, the default one when none is given: every other geography refuses them. `email`
	is where their alerts go; it is written on every call, and one address may sit on several
	users. A new user starts in the default Model Group; `allowed_models` are theirs on top.
	`free` ignores pricing for them — otherwise they are prepaid and blocked until credited.
	`limits` are their rate limits, rows of `metric` (requests, total_tokens), `window` (1m, 1h,
	1d, 1M) and `value`. A new user given none starts under the defaults, 20 requests and 100 000
	tokens a minute; on a known user an empty list lifts them all. Safe to repeat: a known user
	keeps whatever else is not given. → the geography they are pinned to."""
	frappe.only_for(ALLOWED_ROLES)
	grove_user = _set_policy(user, email, allowed_models, geography, free, limits)
	return {"geography": frappe.db.get_value("Grove User", grove_user, "geography")}


@frappe.whitelist()
def provision_key(user: str, title: str = None):
	"""Mint a key for `user`, for the endpoint of the geography they are pinned to. `title` labels
	the key, to tell a user's keys apart."""
	frappe.only_for(ALLOWED_ROLES)
	grove_user = get_grove_user(user)
	geography = frappe.db.get_value("Grove User", grove_user, "geography")
	host = frappe.db.get_value("Geography", geography, "endpoint")
	if not host:
		frappe.throw(f"Geography {geography!r} has no endpoint.")

	# The controller generates the secret and hash, and pushes to the gateways.
	key = frappe.new_doc("Grove API Key")
	key.user = grove_user
	key.title = title
	key.status = "active"
	key.insert()

	return {
		"gateway_url": f"https://{host}",
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def add_credit(user: str, amount: float, note: str = None, reference: str = None):
	"""Append one ledger entry for `user` and settle them. 0 and an unexplained
	negative are refused by the ledger itself. `reference` is the caller's own id for the top-up:
	a call that repeats one adds nothing, so a call that timed out is safe to send again.
	→ their balance after the entry."""
	frappe.only_for(ALLOWED_ROLES)
	grove_user = get_grove_user(user)
	repeated = reference and frappe.db.get_value(
		"Grove Credit", {"reference": reference}, ["grove_user", "amount"], as_dict=True
	)
	if not repeated:
		frappe.get_doc(
			{"doctype": "Grove Credit", "grove_user": grove_user, "amount": amount, "note": note, "reference": reference}
		).insert()
	# A repeat has to be the same top-up: one id on another user or amount is a caller bug.
	elif (repeated.grove_user, flt(repeated.amount, 9)) != (grove_user, flt(amount, 9)):
		frappe.throw(f"Reference {reference!r} already names another top-up.")
	# TODO: we should send a message like it might take some time to reflect
	return {"balance": frappe.db.get_value("Grove User", grove_user, "balance")}


@frappe.whitelist()
def balance(user: str):
	"""What `user` has left: Σ ledger − usage priced so far, as of the last pull
	(up to an hour behind the gateways; `pull_usage` first for a fresh figure). A free user is
	never charged and never gated: their `spent` does not move."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.pricing import credit_summary

	grove_user = get_grove_user(user)
	summary = credit_summary(grove_user)
	return {
		"balance": float(summary["remaining"]),
		"spent": float(summary["spent"]),
		"is_free_user": bool(frappe.db.get_value("Grove User", grove_user, ["free"])),
	}


@frappe.whitelist()
def limits(user: str):
	"""The rate limits of `user`, rows of `metric`, `window` and `value`, as the gateway counts
	them across every key they hold. Nothing for a user Grove does not know."""
	frappe.only_for(ALLOWED_ROLES)
	if not (grove_user := for_reference(user)):
		return []
	return frappe.get_list(
		"Model Limit",
		filters={"parent": grove_user},
		fields=["metric", "window", "value"],
		parent_doctype="Grove User",
		order_by="metric asc, window asc",
	)


@frappe.whitelist()
def pull_usage(user: str):
	"""Drain the keys of `user` from every store now, waiting for a pull in
	flight — for a fresh `balance`. PULLS_PER_HOUR per user. → the Pathway Sync that logged it,
	or None when there was nothing to drain."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.pathway.usage import pull_all

	# TODO: this needs to trigger it but not sync it in the web thread
	grove_user = get_grove_user(user)
	_count_pull(grove_user)
	return {"sync": pull_all(trigger="Manual", wait=60, user=grove_user)}


@frappe.whitelist()
def revoke_key(api_key: str):
	"""Revoke by the full key, not the doc name. The row stays as the record it existed; a revoked
	key is no longer projected, so the next sync prunes it from every proxy."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.grove_api_key.grove_api_key import hash_secret

	key = frappe.db.get_value("Grove API Key", {"key_hash": hash_secret(api_key.strip())})
	if not key:
		frappe.throw("no such API key", frappe.DoesNotExistError)

	frappe.get_doc("Grove API Key", key).revoke()
	return "Revoked. Might take some time to reflect."


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
def usage(
	users: list[str] | str, from_date: str | None = None, to_date: str | None = None,
	period: str | None = None, month: str | None = None, key_hash: str | None = None,
):
	"""Requests and cost per user and per model over a UTC date range: `from_date`/`to_date`, a
	`period` (Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month) or a `month`
	(YYYY-MM); This Month when none is given. `users` are the caller's references, and each one's
	totals come back under it. Summed by the database in one grouped query. `cost`
	is what was charged, so usage while the user was Free adds requests and no cost.
	`daily_summary` is the per-model summary again, per UTC day, for a chart. `as_of` is when the
	newest usage in the range was pulled, in UTC. `key_hash` narrows it all to one key of theirs."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.doctype.usage_record.usage_record import usage_table
	from grove.pathway.routes import utc_timestamp

	if isinstance(users, str):
		users = [users]
	from_date, to_date = usage_window(from_date, to_date, period, month)
	# In and out by reference; the records themselves are keyed by Grove User.
	references = dict(
		frappe.get_list("Grove User", {"reference": ("in", users)}, ["name", "reference"], as_list=True)
	)
	api_key = _key_of(key_hash, list(references)) if key_hash else None
	rows = frappe.db.sql(
		f"""select r.user, u.model, sum(u.requests) as requests,
		sum(if(r.billed, u.grove_cost, 0)) as cost, max(r.creation) as as_of
		from `tabUsage Record` r, {usage_table()}
		where r.user in %(users)s and r.day between %(from_date)s and %(to_date)s {_key_condition(api_key)}
		group by r.user, u.model""",
		{"users": list(references) or [""], "from_date": from_date, "to_date": to_date, "api_key": api_key},
		as_dict=True,
	) if references else []
	per_model = [
		{"model": row.model, "user": row.user, "requests": int(row.requests or 0), "cost": float(row.cost or 0)}
		for row in rows
	]
	totals = {reference: {"requests": 0, "cost": 0.0} for reference in references.values()}
	for row in per_model:
		for field in USAGE_FIELDS:
			totals[references[row["user"]]][field] += row[field]
	return {
		"users": users, "from_date": str(from_date), "to_date": str(to_date),
		"as_of": utc_timestamp(max(row.as_of for row in rows)) if rows else None,
		"model_summary": _totals_by_model(per_model),
		"daily_summary": _daily_summary(list(references), from_date, to_date, api_key) if references else [],
		**totals,
	}


@frappe.whitelist()
def available_models(user: str = None):
	"""Every published model in the default geography, one row per key, `name` being the key a
	caller sends. A catalogue, not an entitlement list — the gateway is what enforces which of
	these a given API key may actually call. With `user`, only the ones they may
	call, as their own geography serves them, to show a person what their keys reach: nothing for
	a user Grove does not know. `dialects` names the surfaces (openai, anthropic) each one answers
	on there."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.model.model import get_modalities
	from grove.pathway.routes import get_dialects, models_in

	geography = frappe.db.get_value("Geography", {"is_default": 1})
	reachable = None
	if user:
		from grove.access import get_reachable_models

		grove_user = for_reference(user)
		reachable = set(get_reachable_models(grove_user))
		if not reachable:
			return []
		geography = frappe.db.get_value("Grove User", grove_user, "geography")
	models = [
		m for m in models_in(geography)
		if m.published and (reachable is None or m.model_key in reachable)
	]
	dialects = get_dialects(models, geography)
	modalities = get_modalities([m.name for m in models])
	return [
		{
			"name": m.model_key, "model_id": m.model_id, **modalities[m.name],
			"provider": m.provider, "geography": m.geography, "dialects": dialects[m.name],
		}
		for m in sorted(models, key=lambda m: m.model_key)
	]


def get_grove_user(user):
	"""The Grove User the caller's reference `user` names; an unknown one is refused."""
	grove_user = for_reference(user)
	if not grove_user:
		frappe.throw(f"No Grove User for {user!r}.")
	return grove_user


def _count_pull(grove_user):
	"""Refuse past PULLS_PER_HOUR on-demand pulls of one user. The hour starts at the first."""
	key = frappe.cache.make_key(f"usage_pull:{grove_user}")
	count = frappe.cache.incr(key)
	# Checked every call, not only on the first: a counter left without a TTL would block forever.
	if frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, 60 * 60)
	if count > PULLS_PER_HOUR:
		frappe.throw(f"{PULLS_PER_HOUR} usage pulls an hour per user.", frappe.RateLimitExceededError)


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


def _set_policy(reference, email, models, geography=None, free=False, limits=None):
	"""Write the Grove User policy `reference` names and return its name — the id every key, usage
	record and access lookup carries. `email` is written every time. `models`, when given, replace
	their own Allow; `geography`, when given, pins them there (else they keep theirs, or get the
	default); `free`, when given, waives pricing; `limits`, when given, replace their rate limits."""
	# A blank reference matches nobody, so every call would insert one more user.
	if not reference:
		frappe.throw("A user needs a reference.")
	name = for_reference(reference)
	doc = frappe.get_doc("Grove User", name) if name else frappe.new_doc("Grove User")
	doc.reference, doc.email = reference, email
	if geography:
		doc.geography = geography
	if models:
		from grove.access import model_doc

		doc.allow = []
		for model in models:
			doc.append("allow", {"model": model_doc(model, doc.geography)})
	if free:
		doc.free = 1
	if limits is not None:
		doc.set("limits", limits)
	doc.save()
	return doc.name


def _totals_by_model(rows):
	"""Usage rows folded into one entry per model, costliest first, then busiest. Rows arrive one
	per (user, model), so a model is summed across users rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(
			row["model"], {"model": row["model"], **dict.fromkeys(USAGE_FIELDS, 0)}
		)
		for f in USAGE_FIELDS:
			totals[f] += row.get(f) or 0

	return sorted(per_model.values(), key=lambda totals: (-totals["cost"], -totals["requests"]))


def _key_of(key_hash, grove_users):
	"""The key behind `key_hash`, only when one of these users holds it: another user's key must
	not reveal its usage."""
	name = frappe.db.get_value(
		"Grove API Key", {"key_hash": key_hash, "user": ("in", grove_users or [""])}
	)
	if not name:
		frappe.throw("No such API key for these users.", frappe.DoesNotExistError)
	return name


def _key_condition(api_key):
	return "and r.api_key = %(api_key)s" if api_key else ""


def _daily_summary(grove_users, from_date, to_date, api_key=None):
	"""Requests and cost per UTC day and model across these users, and one key of theirs when
	given, summed by the database, oldest day first. A day with no usage has no entry."""
	from grove.billing.doctype.usage_record.usage_record import usage_table

	rows = frappe.db.sql(
		f"""select r.day, u.model, sum(u.requests) as requests, sum(if(r.billed, u.grove_cost, 0)) as cost
		from `tabUsage Record` r, {usage_table()}
		where r.user in %(users)s and r.day between %(from_date)s and %(to_date)s {_key_condition(api_key)}
		group by r.day, u.model order by r.day, u.model""",
		{"users": grove_users, "from_date": from_date, "to_date": to_date, "api_key": api_key},
		as_dict=True,
	)
	return [
		{"day": str(row.day), "model": row.model, "requests": int(row.requests or 0), "cost": float(row.cost or 0)}
		for row in rows
	]
