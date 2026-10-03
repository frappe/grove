"""What a site starts with: usage counters, geographies, cloud accounts, regions, networks, providers
and vendor models, read from `catalog.json`. Not Frappe fixtures: those delete and re-insert on every migrate, secrets included."""

import json
from pathlib import Path

import frappe

CATALOG = Path(__file__).with_name("catalog.json")


def read(path=None):
	return json.loads(Path(path or CATALOG).read_text())


def insert_missing(path=None):
	"""Insert what the catalog names and the site lacks. Never updates, never deletes."""
	catalog = read(path)
	for counter in catalog.get("counters", []):
		if not frappe.db.exists("Usage Counter", counter["counter_name"]):
			frappe.get_doc({"doctype": "Usage Counter", **counter}).insert()
	for geography in catalog["geographies"]:
		insert_geography(geography)
	# A cloud account ships without its key, for the operator to fill.
	for cloud_provider in catalog.get("cloud_providers", []):
		insert_named("Cloud Provider", cloud_provider, ignore_mandatory=True)
	for region in catalog.get("regions", []):
		insert_named("Region", region)
	for provider in catalog["providers"]:
		if not frappe.db.exists("Model Provider", {"provider_name": provider["provider_name"]}):
			frappe.get_doc({"doctype": "Model Provider", **provider}).insert()
	# A model lands under the record the catalog's own provider entry describes.
	geographies = {p["provider_name"]: p.get("geography") for p in catalog["providers"]}
	for model in catalog["models"]:
		insert_model(model, geographies.get(model["provider"]) or default_geography())


def insert_named(doctype, entry, ignore_mandatory=False):
	"""A doc the catalog names outright, skipped when the site holds one of that name."""
	if frappe.db.exists(doctype, entry["name"]):
		return
	doc = frappe.get_doc({"doctype": doctype, **entry})
	doc.flags.ignore_mandatory = ignore_mandatory
	doc.insert()


def insert_geography(geography):
	"""Endpoint and zone are the operator's to fill, so this one insert skips the mandatory check.
	The default only when the site has none."""
	if frappe.db.exists("Geography", geography["name"]):
		return
	doc = frappe.get_doc({"doctype": "Geography", **geography})
	doc.is_default = int(not frappe.db.exists("Geography", {"is_default": 1}))
	doc.flags.ignore_mandatory = True
	doc.insert()


def insert_model(model, geography):
	"""Under one record of the provider: the one in `geography` — what the catalog's own entry for
	the provider says — else the only one the site holds. Several and none there is a question
	for the operator. A model in a second geography is a second doc added by hand."""
	provider = provider_record(model["provider"], geography)
	if frappe.db.exists("Model", {"provider": provider, "model_id": model["model_id"]}):
		return
	fields = {key: value for key, value in model.items() if key != "rates"}
	frappe.get_doc({"doctype": "Model", **fields, "provider": provider}).insert()


def provider_record(provider_name, geography):
	records = frappe.get_all("Model Provider", {"provider_name": provider_name}, ["name", "geography"])
	preferred = [r.name for r in records if r.geography == geography]
	if preferred:
		return preferred[0]
	if len(records) == 1:
		return records[0].name
	frappe.throw(
		f"The catalog names models under {provider_name}, which the site carries in "
		f"{len(records)} geographies and not in {geography}."
	)


def default_geography():
	return frappe.db.get_value("Geography", {"is_default": 1})


def get_model_name(model):
	"""The entry's model key."""
	return f"{model['provider']}/{model['model_id']}"


def get_rates(model_key, path=None):
	"""The catalog's rate rows for a model key, [] when it prices none."""
	for model in read(path)["models"]:
		if get_model_name(model) == model_key:
			return model.get("rates") or []
	return []
