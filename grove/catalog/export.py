"""Rewrites `catalog.json` from a site. Run by hand:
`bench --site <site> execute grove.catalog.export.write`."""

import json
from pathlib import Path

import frappe

from grove.catalog.seed import CATALOG

# The one geography the catalog ships: a name, its endpoint and zone filled on the site.
MAIN = "Main"
PROVIDER_FIELDS = ("provider_name", "is_self_hosted", "base_url", "anthropic_base_url", "api_version")
MODEL_FIELDS = ("model_id", "upstream_model_id", "modality")


def write(path=None):
	"""Sorted and indented, so a second export of an unchanged site is an empty diff."""
	catalog = {"geographies": [{"name": MAIN}], "providers": get_providers(), "models": get_models()}
	Path(path or CATALOG).write_text(json.dumps(catalog, indent=1, sort_keys=True) + "\n")


def get_providers():
	"""One entry per provider name, a vendor's moved to Main. No key, no rate card."""
	providers = {}
	for row in frappe.get_all("Model Provider", fields=PROVIDER_FIELDS, order_by="creation"):
		entry = {field: row[field] for field in PROVIDER_FIELDS if row[field]}
		if not row.is_self_hosted:
			entry["geography"] = MAIN
		providers.setdefault(row.provider_name, entry)
	return [providers[name] for name in sorted(providers)]


def get_models():
	"""Vendor models only, one entry per key, each with the rows of its Enabled pricing. Never
	`published`. A key held in several geographies exports once: the default geography's doc,
	else whichever sorts first."""
	rows = frappe.get_all(
		"Model",
		filters={"provider_is_self_hosted": 0},
		fields=["name", "model_key", "provider.provider_name as provider_name", "geography", *MODEL_FIELDS],
		order_by="model_key, geography",
	)
	default = frappe.db.get_value("Geography", {"is_default": 1})
	entries = {}
	for row in sorted(rows, key=lambda row: (row.model_key, row.geography != default)):
		entries.setdefault(row.model_key, {"provider": row.provider_name, "rates": get_rates(row.name)}
			| {field: row[field] for field in MODEL_FIELDS if row[field]})
	return list(entries.values())


def get_rates(model):
	pricing = frappe.db.get_value("Model Pricing", {"model": model, "status": "Enabled"})
	if not pricing:
		return []
	return frappe.get_all(
		"Model Pricing Rate",
		filters={"parent": pricing},
		fields=["counter", "rate"],
		order_by="idx",
		parent_doctype="Model Pricing",
	)
