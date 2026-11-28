# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import functools

import frappe
from frappe.utils import flt

from grove.access import limits_in_force, team_limits
from grove.api.control import for_control_clients
from grove.api.teams import get_team
from grove.utils import utc_timestamp

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


def changes_keys(endpoint):
	@functools.wraps(endpoint)
	def wrapper(team: str, *args, **kwargs):
		if frappe.get_list("Grove Team", filters={"name": team, "suspended": 1}, limit=1):
			frappe.throw("This team is suspended: this operation cannot be performed until that is lifted.")
		return endpoint(team, *args, **kwargs)

	return for_control_clients(wrapper)


@frappe.whitelist()
@changes_keys
def provision_key(
	team: str,
	title: str | None = None,
	geography: str | None = None,
	cap: float | None = None,
	expiry: str | None = None,
	model_groups: list[str] | None = None,
):
	key = frappe.new_doc("Grove API Key")
	key.team, key.title, key.status = get_team(team), title, "active"
	if geography:
		key.geography = geography
	if expiry:
		key.expiry = expiry
	key.set("model_groups", [{"model_group": group} for group in model_groups or []])
	if cap is not None:
		key.cap = cap
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
@changes_keys
def update_key(team: str, key: str, cap: float | None = None):
	doc = get_api_key(team, key)
	if cap is not None:
		doc.cap = cap
	doc.save()
	return key_rows([doc.name])[0]


@frappe.whitelist()
@changes_keys
def update_caps(team: str, caps: dict[str, float]):
	"""`caps` is {key: USD}. Lowered caps are saved first, so moving a cap from one key to another
	fits the spend limit midway as well; any refusal leaves every cap as it was. What the limit
	holds is this month's spend and the caps together (`pricing.committed`), with what the boxes
	count read under the team row: a cap a key already spent frees nothing when moved. Checked once
	for the batch; no save checks again, and a batch that raises nothing asks no box."""
	from grove.billing.pricing import committed, usd
	from grove.pathway.usage import peek_team

	team = get_team(team)
	docs = [get_api_key(team, key) for key in caps]
	if revoked := [doc.name for doc in docs if doc.status != "active"]:
		frappe.throw(f"{', '.join(revoked)} is revoked.")
	raised = any(flt(caps[doc.name]) > flt(doc.cap) for doc in docs)
	team_doc = team_limits(team, for_update=True)
	# A Total team has no spend limit: its caps are for life, the month binds nothing.
	if raised and team_doc.key_cap_window != "Total":
		proposed = {doc.name: flt(caps[doc.name]) for doc in docs}
		total = committed(team, proposed, box_spent=peek_team(team))
		if total > flt(team_doc.spend_limit, 9):
			frappe.throw(
				f"This month's spend and caps would add up to {usd(total)} of a spend limit of {usd(team_doc.spend_limit)}."
			)
	for doc in sorted(docs, key=lambda doc: flt(caps[doc.name]) - flt(doc.cap)):
		doc.cap = flt(caps[doc.name])
		doc.flags.cap_checked = True
		doc.save()
	return key_rows([doc.name for doc in docs])


@frappe.whitelist()
@changes_keys
def revoke_key(team: str, key: str):
	get_api_key(team, key).revoke()
	return "Revoked. Might take some time to reflect."


@frappe.whitelist()
@changes_keys
def revoke_keys(team: str, keys: list[str]):
	docs = [get_api_key(team, key) for key in frappe.parse_json(keys)]
	for doc in docs:
		doc.revoke()
	return [doc.name for doc in docs]


@frappe.whitelist()
@for_control_clients
def transfer_key(team: str, key: str, to_team: str, title: str | None = None):
	"""What Central sends when a trial converts, so the secret the customer's server holds keeps
	working."""
	to_team = get_team(to_team)
	# A retry after a lost reply finds the key moved already: answered as it is, nothing moves.
	if frappe.db.get_value("Grove API Key", key or "", "team") == to_team:
		return key_rows([key])[0]
	doc = get_api_key(team, key)
	doc.transfer(to_team, title)
	return key_rows([doc.name])[0]


def get_gateway_url(geography):
	host = frappe.db.get_value("Geography", geography, "endpoint")
	return f"https://{host}" if host else None


def get_api_key(team, key, ptype="write"):
	names = frappe.get_list("Grove API Key", filters={"name": key or "", "team": team or ""}, pluck="name")
	if not names:
		frappe.throw("no such API key", frappe.DoesNotExistError)
	return frappe.get_doc("Grove API Key", names[0], check_permission=ptype)


def key_rows(names):
	from grove.billing.pricing import month_spent_by

	if not names:
		return []
	rows = frappe.get_list(
		"Grove API Key",
		filters={"name": ("in", names)},
		fields=[*KEY_FIELDS, "team"],
		order_by="creation desc",
	)
	month = month_spent_by("api_key", names)
	teams = frappe.get_list(
		"Grove Team", filters={"name": ("in", {row.team for row in rows})}, fields=["name", "tier"]
	)
	limits = limits_in_force({team.name: team.tier for team in teams})
	for row in rows:
		# A client reads `limits` off every key: the rows its team runs under, never missing.
		row["limits"] = limits.get(row.pop("team"), [])
		row["gateway_url"] = get_gateway_url(row.geography)
		row["month_spent"] = flt(month.get(row.name, 0), 9)
		row["expires_at"] = utc_timestamp(row.expires_at) if row.expires_at else None
		row["expiry_from_first_use"] = bool(row.expiry_from_first_use)
	rows.sort(key=lambda row: row.status == "revoked")
	return rows
