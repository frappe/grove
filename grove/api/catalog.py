# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

from grove.api.control import ALLOWED_ROLES


@frappe.whitelist()
def geographies():
	frappe.only_for(ALLOWED_ROLES)
	rows = frappe.get_list(
		"Geography", fields=["name", "label", "endpoint", "is_default"], order_by="name asc"
	)
	return [
		{
			"name": r.name,
			"label": r.label or r.name,
			"is_default": bool(r.is_default),
			"endpoint": f"https://{r.endpoint}" if r.endpoint else None,
		}
		for r in rows
	]


@frappe.whitelist()
def available_models(team: str, geography: str | None = None):
	"""The published models a key of `team` in `geography` (the default one when none) may call:
	the team's own reach, never another team's, nor the default group's unless the team is in it,
	nor a key's own groups (a trial's)."""
	frappe.only_for(ALLOWED_ROLES)
	from grove.access import get_reachable_models
	from grove.grove.doctype.model.model import get_modalities
	from grove.pathway.routes import get_dialects, models_in

	geography = geography or frappe.db.get_value("Geography", {"is_default": 1})
	reachable = set(get_reachable_models(team, geography))
	if not reachable:
		return []
	models = [m for m in models_in(geography) if m.published and m.model_key in reachable]
	dialects = get_dialects(models, geography)
	modalities = get_modalities([m.name for m in models])
	return [
		{
			"name": m.model_key,
			"model_id": m.model_id,
			"display_name": m.display_name or m.model_id,
			**modalities[m.name],
			"provider": m.provider_name,
			"zero_data_retention": bool(m.zero_data_retention),
			"geography": m.geography,
			"dialects": dialects[m.name],
		}
		for m in sorted(models, key=lambda m: m.model_key)
	]
