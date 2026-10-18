# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac

import frappe
from frappe.integrations.doctype.webhook.webhook import WEBHOOK_SECRET_HEADER
from frappe.model.rename_doc import rename_doc
from frappe.utils import get_datetime
from frappe.utils.password import get_decrypted_password

from grove.access import team_limits
from grove.api.control import ALLOWED_ROLES, CONTROL_ROLE

CONTROL_KEY_HEADER = "X-Grove-Control-Key"


@frappe.whitelist()
def provision_team(team: str, free: bool = False):
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
def update_team(
	team: str,
	suspended: bool | None = None,
	free: bool | None = None,
	tier: str | None = None,
	observed_at: str | None = None,
):
	frappe.only_for(ALLOWED_ROLES)
	from grove.grove.doctype.tier.tier import ensure

	doc = frappe.get_doc("Central Team", get_team(team))
	if is_not_newer(observed_at, doc.last_reported_at):
		return {"team": doc.name, "ignored": "not newer", **team_limits(doc.name)}
	if tier:
		ensure(tier)
	fields = (("free", free), ("suspended", suspended), ("tier", tier), ("last_reported_at", observed_at))
	for field, value in fields:
		if value is not None:
			doc.set(field, value)
	doc.save()
	return {"team": doc.name, **team_limits(doc.name)}


def is_not_newer(observed_at, last_reported_at) -> bool:
	return bool(observed_at and last_reported_at) and get_datetime(observed_at) <= get_datetime(
		last_reported_at
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def team_webhook(
	team: str | None = None,
	tier: str | None = None,
	suspended: bool | None = None,
	observed_at: str | None = None,
):
	"""Where Central's Webhooks land: the team's tier label (and, when Central sends it, its
	standing) as Central read them at `observed_at`, the body signed with the control credential.
	A team Grove does not know has AI off; it is ignored, not refused, so Central stops retrying."""
	user = _webhook_user()
	frappe.set_user(user)
	if not team or not frappe.db.exists("Central Team", team):
		return None
	return update_team(team, suspended=suspended, tier=tier, observed_at=observed_at)


def _webhook_user() -> str:
	"""The control user whose `api_secret` signed this request, or refused. `X-Grove-Control-Key`
	names it by its `api_key`; Frappe's Webhook header carries the HMAC-SHA256 of the raw body."""
	key = frappe.get_request_header(CONTROL_KEY_HEADER)
	user = key and frappe.db.get_value("User", {"api_key": key, "enabled": 1}, "name")
	secret = user and get_decrypted_password("User", user, "api_secret", raise_exception=False)
	if not secret or CONTROL_ROLE not in frappe.get_roles(user):
		frappe.throw("Invalid webhook signature.", frappe.AuthenticationError)
	digest = hmac.new(secret.encode(), frappe.request.get_data(), hashlib.sha256).digest()
	signature = base64.b64encode(digest).decode()
	if not hmac.compare_digest(signature, str(frappe.get_request_header(WEBHOOK_SECRET_HEADER))):
		frappe.throw("Invalid webhook signature.", frappe.AuthenticationError)
	return user


@frappe.whitelist()
def rename_tier(old: str, new: str):
	frappe.only_for(ALLOWED_ROLES)
	if old == new or not frappe.db.exists("Tier", old):
		return
	if frappe.db.exists("Tier", new):
		frappe.throw(f"Tier {new!r} already exists at Grove; rename or delete it first.")
	rename_doc("Tier", old, new, ignore_permissions=True, show_alert=False)


def get_team(team):
	if not team or not frappe.db.exists("Central Team", team):
		frappe.throw(f"No Central Team {team!r}.")
	return team
