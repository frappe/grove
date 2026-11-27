"""What a site starts with, read from `catalog.json`. Not Frappe fixtures: those delete and
re-insert on every migrate, secrets included."""

import json
from pathlib import Path

import frappe

from grove.access import LIMIT_TABLES
from grove.grove.doctype.model_id.model_id import ensure_model_id

CATALOG = Path(__file__).with_name("catalog.json")
MODALITY_FIELDS = ("input_modalities", "output_modalities")


def read(path=None):
	return json.loads(Path(path or CATALOG).read_text())


def insert_missing(path=None):
	"""Insert what the catalog names and the site lacks. Never updates a field, never deletes: a
	held group keeps the rows it has."""
	catalog = read(path)
	for counter in catalog.get("counters", []):
		if not frappe.db.exists("Usage Counter", counter["counter_name"]):
			frappe.get_doc({"doctype": "Usage Counter", **counter}).insert()
	for modality in catalog.get("modalities", []):
		insert_named("Modality", modality)
	for geography in catalog["geographies"]:
		insert_geography(geography)
	for cloud_provider in catalog.get("cloud_providers", []):
		insert_named("Cloud Provider", cloud_provider, ignore_mandatory=True)
	for region in catalog.get("regions", []):
		insert_named("Region", region)
	for provider in catalog["providers"]:
		if not frappe.db.exists("Model Provider", {"provider_name": provider["provider_name"]}):
			frappe.get_doc({"doctype": "Model Provider", **provider}).insert()
	for denied in catalog.get("denied_tools", []):
		if not frappe.db.exists("Denied Tool", denied):
			frappe.get_doc({"doctype": "Denied Tool", **denied}).insert()
	geographies = {p["provider_name"]: p.get("geography") for p in catalog["providers"]}
	docs = {
		get_model_key(model): insert_model(model, geographies.get(model["provider"]) or default_geography())
		for model in catalog["models"]
	}
	for group in catalog.get("model_groups", []):
		insert_model_group(group, docs)
	for tier in catalog.get("tiers", []):
		insert_tier(tier)
	if (default := catalog.get("default_tier")) and not frappe.db.get_single_value(
		"Grove Settings", "default_tier"
	):
		frappe.db.set_single_value("Grove Settings", "default_tier", default)


def tier_rows(entry):
	"""A catalog tier's rows as its doc holds them: a model key becomes one row per doc of it."""
	rows = []
	for row in entry.get("limits", []):
		limit = {field: row[field] for field in ("metric", "window", "value")}
		if not row.get("model_key"):
			rows.append(limit)
			continue
		filters = {"model_key": row["model_key"]}
		if row.get("geography"):
			filters["geography"] = row["geography"]
		models = frappe.get_all("Model", filters=filters, pluck="name")
		if not models:
			frappe.throw(f"Tier {entry['name']}: no model matches the limit row {row}.")
		rows += [{**limit, "model": model} for model in models]
	return rows


def insert_tier(entry):
	"""A tier the site lacks, with the file's rows in the table its scope counts with. One the
	site holds is left as the operator has it."""
	if frappe.db.exists("Tier", entry["name"]):
		return
	rows = tier_rows(entry)
	table = LIMIT_TABLES[entry.get("limit_scope") or "Model"]
	if table == "key_limits" and any(row.get("model") for row in rows):
		frappe.throw(f"Tier {entry['name']} counts per key, every model together: its rows name no model.")
	frappe.get_doc({"doctype": "Tier", **entry, "limits": [], table: rows}).insert()


def insert_named(doctype, entry, ignore_mandatory=False):
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
	provider = provider_record(model["provider"], geography)
	model_id = ensure_model_id(model["model_id"], model.get("display_name"))
	held = frappe.db.exists("Model", {"provider": provider, "model_id": model_id})
	if held:
		return held
	fields = {key: value for key, value in model.items() if key not in ("rates", "display_name")}
	for fieldname in MODALITY_FIELDS:
		fields[fieldname] = [{"modality": word} for word in model.get(fieldname, [])]
	return (
		frappe.get_doc({"doctype": "Model", **fields, "model_id": model_id, "provider": provider})
		.insert()
		.name
	)


def insert_model_group(group, docs):
	"""A group the site lacks, in the geography its models landed in — `docs` is their doc per
	key. That geography's default only when it has none."""
	if frappe.db.exists("Model Group", group["name"]):
		return

	rows = [{"model": docs[key]} for key in group["models"]]
	landed = frappe.db.get_value("Model", rows[0]["model"], "geography") if rows else None
	geography = landed or default_geography()
	is_default = int(not frappe.db.exists("Model Group", {"is_default": 1, "geography": geography}))
	fields = {**group, "models": rows, "geography": geography, "is_default": is_default}
	frappe.get_doc({"doctype": "Model Group", **fields}).insert()


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


def get_model_key(model):
	return f"{model['provider']}/{model['model_id']}"


def get_rates(model_key, path=None):
	for model in read(path)["models"]:
		if get_model_key(model) == model_key:
			return model.get("rates") or []
	return []
