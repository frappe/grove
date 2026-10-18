# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe

from grove.api.control import ALLOWED_ROLES
from grove.api.keys import get_api_key


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
def available_models(team: str | None = None, key: str | None = None, geography: str | None = None):
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
		reachable = (
			set(model_rows("Model Group", [default]).get(default, {}).get("models", [])) if default else set()
		)
	if not reachable:
		return []
	models = [m for m in models_in(geography) if m.published and m.model_key in reachable]
	dialects = get_dialects(models, geography)
	modalities = get_modalities([m.name for m in models])
	return [
		{
			"name": m.model_key,
			"model_id": m.model_id,
			**modalities[m.name],
			"provider": m.provider_name,
			"geography": m.geography,
			"dialects": dialects[m.name],
		}
		for m in sorted(models, key=lambda m: m.model_key)
	]
