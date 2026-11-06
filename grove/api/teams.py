# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac

import frappe
from frappe.auth import LoginAttemptTracker
from frappe.integrations.doctype.webhook.webhook import WEBHOOK_SECRET_HEADER
from frappe.model.rename_doc import rename_doc
from frappe.utils import get_datetime
from frappe.utils.password import get_decrypted_password

from grove.access import team_limits
from grove.api.control import CONTROL_ROLE, for_control_clients

CONTROL_KEY_HEADER = "X-Grove-Control-Key"


@frappe.whitelist()
@for_control_clients
def provision_team(team: str):
	if not team:
		frappe.throw("A team needs an id.")
	if frappe.db.exists("Grove Team", team):
		doc = frappe.get_doc("Grove Team", team)
	else:
		doc = frappe.get_doc({"doctype": "Grove Team", "__newname": team})
	doc.save()
	return {"team": doc.name}


@frappe.whitelist()
@for_control_clients
def update_team(
	team: str,
	suspended: bool | None = None,
	tier: str | None = None,
	observed_at: str | None = None,
	log_payloads: bool | None = None,
):
	from grove.grove.doctype.tier.tier import ensure

	# Checked at the load: an older report is answered below without a save.
	doc = frappe.get_doc("Grove Team", get_team(team), check_permission="write")
	if is_not_newer(observed_at, doc.last_reported_at):
		return {"team": doc.name, "ignored": "not newer", **team_limits(doc.name)}
	if tier:
		ensure(tier)
	fields = (
		("suspended", suspended),
		("tier", tier),
		("last_reported_at", observed_at),
		("log_payloads", log_payloads),
	)
	for field, value in fields:
		if value is not None:
			doc.set(field, value)
	doc.save()
	return {"team": doc.name, **team_limits(doc.name)}


@frappe.whitelist()
@for_control_clients
def archive_team(team: str):
	return frappe.get_doc("Grove Team", get_team(team)).revoke_keys()


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
	user = _webhook_user()
	frappe.set_user(user)
	if not team or not frappe.db.exists("Grove Team", team):
		return None
	return update_team(team, suspended=suspended, tier=tier, observed_at=observed_at)


def _webhook_user() -> str:
	"""The control user whose `api_secret` signed this request, or refused. `X-Grove-Control-Key`
	names it by its `api_key`; Frappe's Webhook header carries the HMAC-SHA256 of the raw body.
	Three bad signatures in a row from one address lock it out for five minutes (429), before any lookup."""
	attempts = LoginAttemptTracker(f"webhook:{frappe.local.request_ip}", max_consecutive_login_attempts=2)
	if not attempts.is_user_allowed():
		frappe.throw("Too many bad webhook signatures from this address.", frappe.TooManyRequestsError)
	key = frappe.get_request_header(CONTROL_KEY_HEADER)
	user = key and frappe.db.get_value("User", {"api_key": key, "enabled": 1}, "name")
	secret = user and get_decrypted_password("User", user, "api_secret", raise_exception=False)
	if not secret or CONTROL_ROLE not in frappe.get_roles(user) or not is_signed_with(secret):
		attempts.add_failure_attempt()
		frappe.throw("Invalid webhook signature.", frappe.AuthenticationError)
	attempts.add_success_attempt()
	return user


def is_signed_with(secret: str) -> bool:
	digest = hmac.new(secret.encode(), frappe.request.get_data(), hashlib.sha256).digest()
	signature = base64.b64encode(digest).decode()
	return hmac.compare_digest(signature, str(frappe.get_request_header(WEBHOOK_SECRET_HEADER)))


@frappe.whitelist()
@for_control_clients
def rename_tier(old: str, new: str):
	tiers = frappe.get_list("Tier", filters={"name": ["in", [old, new]]}, pluck="name")
	if old == new or old not in tiers or new in tiers:
		return
	rename_doc("Tier", old, new, ignore_permissions=True, show_alert=False)


def get_team(team):
	"""`team`, when Grove knows it and the caller may read it. A team out of the caller's reach is
	no team to it."""
	if not team or not frappe.get_list("Grove Team", filters={"name": team}, limit=1):
		frappe.throw(f"No Grove Team {team!r}.")
	return team
