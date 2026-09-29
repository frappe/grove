"""What a site starts with: geographies, providers and vendor models, read from `catalog.json`.
Not Frappe fixtures: those delete and re-insert on every migrate, secrets included."""

import json
from pathlib import Path

import frappe

CATALOG = Path(__file__).with_name("catalog.json")


def read(path=None):
	return json.loads(Path(path or CATALOG).read_text())


def insert_missing(path=None):
	"""Insert what the catalog names and the site lacks. Never updates, never deletes."""
	catalog = read(path)
	for geography in catalog["geographies"]:
		insert_geography(geography)
	for provider in catalog["providers"]:
		if not frappe.db.exists("Model Provider", {"provider_name": provider["provider_name"]}):
			frappe.get_doc({"doctype": "Model Provider", **provider}).insert()
	for model in catalog["models"]:
		insert_model(model)


def insert_geography(geography):
	"""Endpoint and zone are the operator's to fill, so this one insert skips the mandatory check.
	The default only when the site has none."""
	if frappe.db.exists("Geography", geography["name"]):
		return
	doc = frappe.get_doc({"doctype": "Geography", **geography})
	doc.is_default = int(not frappe.db.exists("Geography", {"is_default": 1}))
	doc.flags.ignore_mandatory = True
	doc.insert()


def insert_model(model):
	if frappe.db.exists("Model", get_model_name(model)):
		return
	provider = frappe.db.get_value("Model Provider", {"provider_name": model["provider"]})
	if not provider:
		frappe.throw(f"The catalog names {get_model_name(model)} under a provider it does not carry.")
	fields = {key: value for key, value in model.items() if key != "rates"}
	frappe.get_doc({"doctype": "Model", **fields, "provider": provider}).insert()


def get_model_name(model):
	return f"{model['provider']}/{model['model_id']}"


def get_rates(model_name, path=None):
	"""The catalog's rate rows for a Model, [] when it prices none."""
	for model in read(path)["models"]:
		if get_model_name(model) == model_name:
			return model.get("rates") or []
	return []
