# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

from grove.access import team_limits
from grove.api.control import ALLOWED_ROLES

# NOTE: a Central Team is named by its id in Central (its doc name); Grove keeps nothing else about who it is.


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


def get_team(team):
	"""`team`, Central's id, once it is known here; an unknown one is refused."""
	if not team or not frappe.db.exists("Central Team", team):
		frappe.throw(f"No Central Team {team!r}.")
	return team
