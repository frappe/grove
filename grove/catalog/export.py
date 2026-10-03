"""Rewrites `catalog.json` from a site. Run by hand:
`bench --site <site> execute grove.catalog.export.write`."""

import json
from pathlib import Path

import frappe

from grove.access import model_rows
from grove.catalog.seed import CATALOG, get_model_key
from grove.billing.pricing import CounterTable

# The one geography the catalog ships: a name, its endpoint and zone filled on the site.
MAIN = "Main"
PROVIDER_FIELDS = ("provider_name", "is_self_hosted", "base_url", "anthropic_base_url", "api_version")
MODEL_FIELDS = ("model_id", "upstream_model_id", "modality")


def write(path=None):
	"""Sorted and indented, so a second export of an unchanged site is an empty diff."""
	models = get_models()
	catalog = {
		"counters": get_counters(), "geographies": [{"name": MAIN}],
		"cloud_providers": get_cloud_providers(), "regions": get_regions(),
		"providers": get_providers(), "models": models, "model_groups": get_model_groups(models),
	}
	Path(path or CATALOG).write_text(json.dumps(catalog, indent=1, sort_keys=True) + "\n")


def get_counters():
	"""Every counter in table order: roots first, so a load inserts a base before what derives
	from it. A derived row carries no unit — it takes its base's."""
	rows = []
	for row in CounterTable.load().rows:
		entry = {"counter_name": row.name, "label": row.label}
		if row.base_counter:
			entry |= {"base_counter": row.base_counter, "min_prompt_tokens": row.min_prompt_tokens}
		else:
			entry |= {"unit": row.unit} | ({"part_of": row.part_of} if row.part_of else {})
		rows.append({k: v for k, v in entry.items() if v})
	return rows


def get_cloud_providers():
	"""The account's type only: no key, no access key id."""
	return frappe.get_all("Cloud Provider", fields=["name", "provider_type"], order_by="name")


def get_regions():
	"""Every region, moved to Main."""
	rows = frappe.get_all("Region", fields=["name", "label", "cloud_provider"], order_by="name")
	return [{k: v for k, v in row.items() if v} | {"geography": MAIN} for row in rows]


def get_providers():
	"""One entry per provider name, a vendor's moved to Main. No key."""
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


def get_model_groups(models):
	"""Every group with the exported models it grants, by key: ours are not in the file. Never
	`is_default`, nor its geography: a group lands where its models do."""
	exported = {get_model_key(model) for model in models}
	granted = model_rows("Model Group")
	return [
		{k: v for k, v in group.items() if v}
		| {"models": [key for key in granted.get(group.name, {}).get("models", []) if key in exported]}
		for group in frappe.get_all("Model Group", fields=["name", "description"], order_by="name")
	]


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
