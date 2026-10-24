# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import cint, flt

from grove.access import team_limits
from grove.api.control import ALLOWED_ROLES
from grove.api.teams import get_team

KEY_FIELDS = [
	"name",
	"title",
	"status",
	"creation",
	"key_hash",
	"masked",
	"geography",
	"cap",
	"expiry",
	"expiry_from_first_use",
	"expires_at",
]


@frappe.whitelist()
def provision_key(
	team: str,
	title: str | None = None,
	geography: str | None = None,
	cap: float | None = None,
	models: list[str] | None = None,
	limits: list[dict] | None = None,
	expiry: str | None = None,
	expiry_from_first_use: bool = False,
	model_group: str | None = None,
):
	"""Mint a key for `team` in `geography` — the default one when none is given; every other
	geography's gateway refuses it. `expiry` is when it retires on its own, one of the key's rules
	(Never, 1 Day, 7 Days, 14 Days, 30 Days), Never when not given, fixed after; counted from the
	mint, or from the key's first usage record with `expiry_from_first_use`. `model_group` puts it in
	that group instead of its geography's default."""
	frappe.only_for(ALLOWED_ROLES)
	key = frappe.new_doc("Grove API Key")
	key.team, key.title, key.status = get_team(team), title, "active"
	if geography:
		key.geography = geography
	if model_group:
		key.append("model_groups", {"model_group": model_group})
	if expiry:
		key.expiry = expiry
		key.expiry_from_first_use = int(bool(expiry_from_first_use))
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
def update_key(
	team: str,
	key: str,
	cap: float | None = None,
	models: list[str] | None = None,
	limits: list[dict] | None = None,
):
	"""Change what a key of `team` may spend or call: `cap`, `models` (replacing its own Allow)
	and `limits` (replacing its own rate limits; `[]` puts it back under its team's tier alone),
	each kept when not given. Its geography never changes — mint another key for that. → the key as
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
	refusal leaves every cap as it was. What the limit holds is this month's spend and the caps
	together (`pricing.committed`), with what the boxes count read under the team row: a cap a key already
	spent frees nothing when moved. Checked once for the batch; no save checks again. → the keys
	as `keys` lists them."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.billing.pricing import committed, usd
	from grove.pathway.usage import peek_team

	team = get_team(team)
	docs = [get_api_key(team, key) for key in caps]
	if revoked := [doc.name for doc in docs if doc.status != "active"]:
		frappe.throw(f"{', '.join(revoked)} is revoked.")
	raised = any(flt(caps[doc.name]) > flt(doc.cap) for doc in docs)
	team_doc = team_limits(team, for_update=True)
	# A Total team has no spend limit: its caps are for life, the month binds nothing.
	raised = raised and team_doc.key_cap_window != "Total"
	box_spent = peek_team(team) if raised else {}
	total = committed(team, {doc.name: flt(caps[doc.name]) for doc in docs}, box_spent=box_spent)
	if raised and total > flt(team_doc.spend_limit, 9):
		frappe.throw(
			f"This month's spend and caps would add up to {usd(total)} of a spend limit of {usd(team_doc.spend_limit)}."
		)
	for doc in sorted(docs, key=lambda doc: flt(caps[doc.name]) - flt(doc.cap)):
		doc.cap = flt(caps[doc.name])
		doc.flags.cap_checked = True
		doc.save()
	return key_rows([doc.name for doc in docs])


@frappe.whitelist()
def limits(team: str, key: str):
	"""The rate limits of one key of `team`, rows of `metric`, `window` and `value`, as the gateway
	counts them for that key alone: its team's tier's (else the defaults), with its own rows
	replacing the tier's on the metrics they name. Refused for a key the team does not hold."""
	frappe.only_for(ALLOWED_ROLES)
	return limit_rows_of([get_api_key(team, key).name]).get(key, [])


@frappe.whitelist()
def revoke_key(team: str, key: str):
	frappe.only_for(ALLOWED_ROLES)
	get_api_key(team, key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist()
def extend_key(team: str, key: str, days: int):
	"""`days` more on the key's `expires_at`, from now or from its moment, whichever is later; a key
	without a moment is left as it is, a revoked one refused. → the key as `keys` lists it."""
	frappe.only_for(ALLOWED_ROLES)
	doc = get_api_key(team, key)
	doc.extend_expiry(cint(days))
	return key_rows([doc.name])[0]


@frappe.whitelist()
def transfer_key(team: str, key: str, to_team: str, title: str | None = None):
	"""Hand a key of `team` to `to_team` — what Central sends when a trial converts, so the secret
	the customer's server holds keeps working. The team and the title (`title`, when given) move,
	the trial's rules go: the geography's default group in place of its groups, its expiry lifted;
	cap, geography and limit rows stay as minted. Counted against `to_team` like a mint: its key
	count, and the cap within a Monthly team's spend limit. The box closes the old team's books at
	the push. A key `to_team` already holds is answered as it is, so a lost reply is retried
	safely. → the key as `keys` lists it."""
	frappe.only_for(ALLOWED_ROLES)
	to_team = get_team(to_team)
	holders = [get_team(team), to_team]
	name = frappe.db.get_value("Grove API Key", {"name": key or "", "team": ("in", holders)})
	if not name:
		frappe.throw("no such API key", frappe.DoesNotExistError)
	doc = frappe.get_doc("Grove API Key", name)
	doc.transfer(to_team, title)
	return key_rows([doc.name])[0]


@frappe.whitelist()
def secrets(team: str):
	"""Every live key of `team` with its secret, for the control client to hand the team's servers:
	`name`, `title`, `geography`, `gateway_url` and `api_key` each, newest first. A revoked key is
	not in it. An unknown team is refused."""
	frappe.only_for(ALLOWED_ROLES)
	from frappe.utils.password import get_decrypted_password

	rows = frappe.get_list(
		"Grove API Key",
		filters={"team": get_team(team), "status": "active"},
		fields=["name", "title", "geography"],
		order_by="creation desc",
	)
	for row in rows:
		row["gateway_url"] = get_gateway_url(row.geography)
		row["api_key"] = get_decrypted_password("Grove API Key", row.name, "api_secret")
	return rows


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
			doc.append(
				"allow",
				{
					"model": model_doc(
						model, doc.geography or frappe.db.get_value("Geography", {"is_default": 1})
					)
				},
			)
	if limits is not None:
		doc.set("limits", limits)


def key_rows(names):
	"""The keys named, as `keys` lists them: KEY_FIELDS (`masked` is the prefix and four characters,
	kept on the key at mint, so nothing is decrypted here) plus the endpoint, the limits,
	`month_spent`, `revocable_at`, `expires_at` (UTC) and whether the expiry counts from first use. Live keys first, newest first within each. A handful of
	queries however many keys."""
	from grove.billing.pricing import month_spent_by
	from grove.grove.doctype.grove_api_key.grove_api_key import REVOKE_AFTER_HOURS
	from grove.pathway.routes import utc_timestamp

	if not names:
		return []
	rows = frappe.get_list(
		"Grove API Key", filters={"name": ("in", names)}, fields=KEY_FIELDS, order_by="creation desc"
	)
	limits = limit_rows_of(names)
	month = month_spent_by("api_key", names)
	for row in rows:
		row["gateway_url"] = get_gateway_url(row.geography)
		row["limits"] = limits.get(row.name, [])
		row["revocable_at"] = utc_timestamp(frappe.utils.add_to_date(row.creation, hours=REVOKE_AFTER_HOURS))
		row["month_spent"] = flt(month.get(row.name, 0), 9)
		row["expires_at"] = utc_timestamp(row.expires_at) if row.expires_at else None
		row["expiry_from_first_use"] = bool(row.expiry_from_first_use)
	rows.sort(key=lambda row: row.status == "revoked")
	return rows


def limit_rows_of(names):
	"""{key: [{metric, window, value}, ...]} for these keys as the gateway counts them: their
	team's Tier's (else the default tier's, else the defaults), with their own rows replacing the
	tier's on the metrics they name."""
	from grove.access import effective_limits, limit_entries

	keys = frappe.get_list("Grove API Key", filters={"name": ("in", names)}, fields=["name", "team"])
	tiers = frappe.get_list(
		"Grove Team", filters={"name": ("in", [k.team for k in keys] or [""])}, fields=["name", "tier"]
	)
	return effective_limits(keys, limit_entries("Grove API Key", names), {t.name: t.tier for t in tiers})
