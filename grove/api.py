# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

"""Provisioning API. The only user-facing surface: register a user, mint an API key, etc."""

import hmac

import frappe

from grove.grove.doctype.grove_user.grove_user import for_email, register_user
from grove.utils import utc_today

CONTROL_ROLE = "Grove Control"
ALLOWED_ROLES = [CONTROL_ROLE]
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens")
# What a caller sees as prompt tokens: every prompt-side counter, cached or written or plain.
PROMPT_COUNTERS = ("input_tokens", "cached_tokens", "cache_write_tokens", "cache_write_1h_tokens")
TOKEN_COUNTERS = (*PROMPT_COUNTERS, "completion_tokens")


@frappe.whitelist()
def provision_key(name: str, email: str, geography: str, allowed_models: list[str]=None, pin: bool=False, free: bool=False):
	"""Register the user and mint a key for `geography`'s endpoint. `pin` also refuses the user
	everywhere else; `free` ignores pricing for them — otherwise they are prepaid and blocked until
	credited."""
	frappe.only_for(ALLOWED_ROLES)
	# Blank would read as no filter and hand out whichever endpoint comes first.
	host = frappe.db.get_value("Geography", geography, "endpoint") if geography else None
	if not host:
		frappe.throw(f"No Geography named {geography!r}.")

	# Access is per-user, so it lands on the Grove User rather than the key. Written
	# unconditionally: a blank one is the correct fail-closed default.
	grove_user = _set_policy(email, name, allowed_models, geography if pin else None, free)

	# The controller generates the secret and hash, and pushes to the gateways.
	key = frappe.new_doc("Grove API Key")
	key.user = grove_user
	key.status = "active"
	key.insert()

	return {
		"gateway_url": f"https://{host}",
		"api_key": key.get_password("api_secret"),
	}


@frappe.whitelist()
def add_credit(email: str, amount: float, note: str = None):
	"""Append one ledger entry for the user behind `email` and settle them. 0 and an unexplained
	negative are refused by the ledger itself. → their balance after the entry."""
	frappe.only_for(ALLOWED_ROLES)
	grove_user = for_email(email)
	if not grove_user:
		frappe.throw(f"No Grove User for {email!r}.")
	frappe.get_doc({"doctype": "Grove Credit", "grove_user": grove_user, "amount": amount, "note": note}).insert()
	return {"balance": frappe.db.get_value("Grove User", grove_user, "balance")}


@frappe.whitelist()
def balance(email: str):
	"""What the user behind `email` has left: Σ ledger − usage priced so far, as of the last pull
	(up to an hour behind the gateways; `pull_usage` first for a fresh figure). A free user is
	never charged and never gated: their `spent` does not move."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.pricing import credit_summary

	grove_user = for_email(email)
	if not grove_user:
		frappe.throw(f"No Grove User for {email!r}.")
	flags = frappe.db.get_value("Grove User", grove_user, ["free", "credit_exhausted"], as_dict=True)
	summary = credit_summary(grove_user)
	return {
		"balance": float(summary["remaining"]),
		"allocated": float(summary["allocated"]),
		"spent": float(summary["spent"]),
		"free": bool(flags.free),
		"credit_exhausted": bool(flags.credit_exhausted),
	}


@frappe.whitelist()
def pull_usage(email: str | None = None):
	"""Drain now instead of at the next hourly pull, waiting for one in flight: every gateway, or
	only the keys of the user behind `email` from every store — for a fresh `balance`. → the
	Pathway Sync that logged it, or None when there was nothing to drain."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.pathway.usage import pull_all

	grove_user = None
	if email and not (grove_user := for_email(email)):
		frappe.throw(f"No Grove User for {email!r}.")
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
	period: str | None = None, month: str | None = None,
):
	"""Tokens and cost per user and per model over a UTC date range: `from_date`/`to_date`, a
	`period` (Today, Yesterday, Last 7 Days, Last 30 Days, This Month, Last Month) or a `month`
	(YYYY-MM); This Month when none is given. Summed by the database in one grouped query. `cost`
	is what was charged, so usage while the user was Free adds tokens and no cost. `as_of` is when
	the newest usage in the range was pulled, in UTC."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.usage_record.usage_record import usage_table
	from grove.pathway.routes import utc_timestamp

	if isinstance(users, str):
		users = [users]
	from_date, to_date = usage_window(from_date, to_date, period, month)
	# In and out by email; the records themselves are keyed by Grove User.
	emails = dict(frappe.get_list("Grove User", {"user": ("in", users)}, ["name", "user"], as_list=True))
	rows = frappe.db.sql(
		f"""select r.user, u.model, {", ".join(f"sum(u.{c}) as {c}" for c in TOKEN_COUNTERS)},
		sum(if(r.billed, u.grove_cost, 0)) as cost, max(r.creation) as as_of
		from `tabUsage Record` r, {usage_table()}
		where r.user in %(users)s and r.day between %(from_date)s and %(to_date)s
		group by r.user, u.model""",
		{"users": list(emails) or [""], "from_date": from_date, "to_date": to_date},
		as_dict=True,
	) if emails else []
	per_model = [{**_token_totals(row), "model": row.model, "user": row.user} for row in rows]
	totals = {email: dict.fromkeys(USAGE_FIELDS, 0) | {"cost": 0.0} for email in emails.values()}
	for row in per_model:
		for field in (*USAGE_FIELDS, "cost"):
			totals[emails[row["user"]]][field] += row[field]
	return {
		"users": users, "from_date": str(from_date), "to_date": str(to_date),
		"as_of": utc_timestamp(max(row.as_of for row in rows)) if rows else None,
		"model_summary": _totals_by_model(per_model, (*USAGE_FIELDS, "cost")), **totals,
	}


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


@frappe.whitelist()
def available_models():
	"""Every model with a live route. A catalogue, not an entitlement list — the gateway is
	what enforces which of these a given API key may actually call."""
	frappe.only_for(ALLOWED_ROLES)
	return frappe.get_list(
		"Model",
		{"published": 1},
		["name", "model_id", "modality"],
	)


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


def _set_policy(email, full_name, models, geography=None, free=False):
	"""Write the user's Grove User policy and return its name — the id every key, usage
	record and access lookup carries. `models` is exactly what they may call; `geography`, when
	given, pins them; `free`, when given, waives pricing. `full_name` names the login when this is
	the insert that creates it."""
	name = for_email(email)
	doc = frappe.get_doc("Grove User", name) if name else frappe.new_doc("Grove User")
	doc.user = register_user(email, full_name)
	if models:
		doc.allow = []
		for model in models:
			doc.append("allow", {"model": model})
	if geography:
		doc.geography = geography
	if free:
		doc.free = 1
	doc.save()
	return doc.name


def _token_totals(row):
	"""One grouped row as the token columns the endpoint has always returned: prompt is every
	prompt-side counter, cached and completion their own."""
	return {
		"prompt_tokens": sum(int(row.get(c) or 0) for c in PROMPT_COUNTERS),
		"cached_tokens": int(row.get("cached_tokens") or 0),
		"completion_tokens": int(row.get("completion_tokens") or 0),
		"cost": float(row.get("cost") or 0),
	}


def _totals_by_model(rows, fields):
	"""Token rows folded into one entry per model, biggest consumer first. Rows arrive one per
	(record, model) — a user holds several keys, each with its own day records — so a model is
	summed across all of them rather than overwritten."""
	per_model = {}
	for row in rows:
		totals = per_model.setdefault(
			row["model"], {"model": row["model"], **dict.fromkeys(fields, 0)}
		)
		for f in fields:
			totals[f] += row.get(f) or 0

	return sorted(per_model.values(), key=lambda totals: -totals["prompt_tokens"])
