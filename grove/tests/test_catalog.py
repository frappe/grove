# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The catalog inserts what a site lacks and leaves what it holds; its export carries no secret
and loads back to the same file."""

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.billing.doctype.model_pricing.test_model_pricing import enabled_pricing
from grove.catalog import export, seed
from grove.grove.doctype.model_provider.test_model_provider import keyed_provider, our_model, vendor_model
from grove.infrastructure.doctype.geography.test_geography import make_test_geography

COUNTED = (
	"Usage Counter",
	"Geography",
	"Cloud Provider",
	"Region",
	"Model Provider",
	"Denied Tool",
	"Model",
	"Model Pricing",
	"Model Group",
)


class CatalogCase(IntegrationTestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.path = Path(directory.name) / "catalog.json"

	def load(self, **catalog):
		self.path.write_text(json.dumps({"geographies": [], "providers": [], "models": []} | catalog))
		seed.insert_missing(self.path)


class TestInsertMissing(CatalogCase):
	CATALOG: ClassVar[dict] = {
		"counters": [{"counter_name": "web_search_requests", "label": "Web searches", "unit": "request"}],
		"geographies": [{"name": "Catalog"}],
		"cloud_providers": [{"name": "catalog-cloud", "provider_type": "aws"}],
		"regions": [
			{
				"name": "catalog-south-1",
				"label": "Catalog South",
				"geography": "Catalog",
				"cloud_provider": "aws",
			}
		],
		"providers": [
			{
				"provider_name": "_T-catalog-vendor",
				"display_name": "Catalog Vendor",
				"geography": "Catalog",
				"base_url": "https://api.vendor.test/v1",
				"extra_headers": [{"header": "x-catalog", "value": "1"}],
			}
		],
		"denied_tools": [{"provider_name": "_T-catalog-vendor", "tool": "web_search_20250305"}],
		"models": [
			{
				"provider": "_T-catalog-vendor",
				"model_id": "big-1",
				"display_name": "Big One",
				"upstream_model_id": "big-1-2026",
				"rates": [{"counter": "prompt_tokens", "rate": 2.5}],
			}
		],
		"model_groups": [
			{"name": "catalog-group", "description": "Catalog", "models": ["_T-catalog-vendor/big-1"]}
		],
	}

	def test_a_providers_display_name_and_headers_and_a_models_display_name_land(self):
		self.load(**self.CATALOG)
		[name] = frappe.get_all(
			"Model Provider", filters={"provider_name": "_T-catalog-vendor"}, pluck="name"
		)
		vendor = frappe.get_doc("Model Provider", name)
		self.assertEqual(("Catalog Vendor", {"x-catalog": "1"}), (vendor.display_name, vendor.headers))
		self.assertEqual("Big One", frappe.db.get_value("Model ID", "big-1", "display_name"))
		# A held model's Model ID takes the name it lacks; a name the operator set stays.
		frappe.db.set_value("Model ID", "big-1", "display_name", "")
		self.load(**self.CATALOG)
		self.assertEqual("Big One", frappe.db.get_value("Model ID", "big-1", "display_name"))
		frappe.db.set_value("Model ID", "big-1", "display_name", "Big One, Renamed")
		self.load(**self.CATALOG)
		self.assertEqual("Big One, Renamed", frappe.db.get_value("Model ID", "big-1", "display_name"))

	def test_what_is_absent_is_inserted_once_and_never_its_pricing(self):
		make_test_geography()
		default = frappe.db.get_value("Geography", {"is_default": 1})
		self.load(**self.CATALOG)
		geography = frappe.get_doc("Geography", "Catalog")
		self.assertFalse(geography.endpoint or geography.fleet_zone or geography.is_default)
		self.assertEqual(frappe.db.get_value("Geography", {"is_default": 1}), default)
		model = frappe.get_doc("Model", {"model_key": "_T-catalog-vendor/big-1"})
		self.assertEqual(
			(model.upstream_model_id, model.published, model.provider_is_self_hosted), ("big-1-2026", 0, 0)
		)
		self.assertFalse(frappe.db.exists("Model Pricing", {"model": model.name}))
		self.assertEqual(frappe.db.get_value("Usage Counter", "web_search_requests", "unit"), "request")
		self.assertTrue(frappe.db.exists("Denied Tool", "_T-catalog-vendor:web_search_20250305"))
		cloud = frappe.get_doc("Cloud Provider", "catalog-cloud")
		self.assertEqual(
			(cloud.resource_type, cloud.get_password("api_key", raise_exception=False)), ("Machine", None)
		)
		self.assertEqual(frappe.db.get_value("Region", "catalog-south-1", "geography"), "Catalog")
		group = frappe.get_doc("Model Group", "catalog-group")
		self.assertEqual(
			([row.model_key for row in group.models], group.geography, group.is_default),
			(["_T-catalog-vendor/big-1"], "Catalog", 1),
		)
		counts = [frappe.db.count(doctype) for doctype in COUNTED]
		self.load(**self.CATALOG)
		self.assertEqual([frappe.db.count(doctype) for doctype in COUNTED], counts)

	def test_what_a_held_group_lists_is_left_as_it_is(self):
		# Named to run after the test above: the class rolls back once, and that one asserts the
		# first group in the geography is the default and counts rows.
		make_test_geography()
		group = {"name": "catalog-grown", "description": "Grown", "models": ["_T-catalog-vendor/big-1"]}
		self.load(**{**self.CATALOG, "model_groups": [group]})
		grown_2 = {"provider": "_T-catalog-vendor", "model_id": "grown-2", "rates": []}
		grown = {
			**self.CATALOG,
			"models": [*self.CATALOG["models"], grown_2],
			"model_groups": [{**group, "models": ["_T-catalog-vendor/big-1", "_T-catalog-vendor/grown-2"]}],
		}
		self.load(**grown)
		self.assertTrue(frappe.db.exists("Model", {"model_key": "_T-catalog-vendor/grown-2"}))
		rows = [row.model_key for row in frappe.get_doc("Model Group", "catalog-grown").models]
		self.assertEqual(rows, ["_T-catalog-vendor/big-1"])

	def test_a_provider_the_site_holds_is_left_as_it_is(self):
		held = keyed_provider("_T-catalog-held", "held-secret", base_url="https://held.test/v1")
		self.load(
			providers=[
				{
					"provider_name": "_T-catalog-held",
					"geography": "Catalog",
					"base_url": "https://new.test/v1",
				}
			]
		)
		[name] = frappe.get_all("Model Provider", filters={"provider_name": "_T-catalog-held"}, pluck="name")
		doc = frappe.get_doc("Model Provider", name)
		self.assertEqual(
			(doc.name, doc.base_url, doc.geography, doc.enabled_keys[0].get_password("api_key")),
			(held.name, "https://held.test/v1", held.geography, "held-secret"),
		)

	def test_a_model_under_a_provider_nobody_carries_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self.load(models=[{"provider": "_T-catalog-nobody", "model_id": "big-1"}])


class TestASiteWithNoDefaultGeography(CatalogCase):
	"""Its own class: the rollback is once per class, and this one moves the default."""

	def test_the_first_geography_inserted_is_the_default(self):
		frappe.db.set_value("Geography", {"is_default": 1}, "is_default", 0)
		self.load(geographies=[{"name": "Catalog-First"}, {"name": "Catalog-Second"}])
		self.assertEqual(
			frappe.get_all("Geography", filters={"is_default": 1}, pluck="name"), ["Catalog-First"]
		)


class TestExport(CatalogCase):
	RATES: ClassVar[dict] = {"prompt_tokens": 2.5, "completion_tokens": 15, "prompt_tokens_above_272k": 5}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		vendor = keyed_provider(
			"_T-catalog-export",
			"export-secret",
			base_url="https://export.test/v1",
			display_name="Catalog Export",
			extra_headers=[{"header": "x-export", "value": "1"}],
		)
		cls.provider = vendor.name
		cls.model = vendor_model("big-2", vendor.name).insert().name
		frappe.db.set_value("Model ID", "big-2", "display_name", "Big Two")
		cls.pricing = enabled_pricing(cls.model, **cls.RATES).name
		cls.ours = our_model("catalog-ours-7b").insert().name
		rows = [{"model": cls.model}, {"model": cls.ours}]
		group = {"name": "catalog-export-group", "geography": vendor.geography, "models": rows}
		frappe.get_doc({"doctype": "Model Group", **group}).insert()

	def exported(self):
		export.write(self.path)
		return self.path.read_text()

	@staticmethod
	def key(model):
		return frappe.db.get_value("Model", model, "model_key")

	def test_no_secret_no_model_of_ours_and_no_geography_but_main(self):
		text = self.exported()
		catalog = json.loads(text)
		for key in ("api_key", "export-secret", "published", "access_key_id", "vpc_id", "cidr_block"):
			self.assertNotIn(f'"{key}"', text)
		self.assertEqual(catalog["geographies"], [{"name": "Main"}])
		self.assertEqual({r["geography"] for r in catalog["regions"]}, {"Main"})
		self.assertEqual(catalog["counters"], seed.read()["counters"])
		self.assertEqual(
			{p.get("geography") for p in catalog["providers"] if not p.get("is_self_hosted")}, {"Main"}
		)
		[model] = [m for m in catalog["models"] if seed.get_model_key(m) == self.key(self.model)]
		self.assertEqual(model["rates"], [{"counter": c, "rate": r} for c, r in self.RATES.items()])
		self.assertEqual(model["display_name"], "Big Two")
		[vendor] = [p for p in catalog["providers"] if p["provider_name"] == "_T-catalog-export"]
		self.assertEqual(vendor["display_name"], "Catalog Export")
		self.assertEqual(vendor["extra_headers"], [{"header": "x-export", "value": "1"}])
		self.assertNotIn(self.key(self.ours), [seed.get_model_key(m) for m in catalog["models"]])
		[group] = [g for g in catalog["model_groups"] if g["name"] == "catalog-export-group"]
		self.assertEqual(group["models"], [self.key(self.model)])

	def test_a_modality_exports_its_description_only_when_it_has_one(self):
		words = [
			{"name": "Catalog-scan", "is_input": 1, "is_output": 0, "description": "A scanned page."},
			{"name": "Catalog-plain", "is_input": 1, "is_output": 0},
		]
		self.load(modalities=words)
		exported = {m["name"]: m for m in json.loads(self.exported())["modalities"]}
		self.assertEqual(exported["Catalog-scan"], words[0])
		self.assertEqual(exported["Catalog-plain"], words[1])

	def test_the_file_loaded_where_its_entries_are_missing_exports_the_same(self):
		first = self.exported()
		key = self.key(self.model)
		frappe.db.delete("Model Pricing Rate", {"parent": self.pricing})
		frappe.db.delete("Model Pricing", {"name": self.pricing})
		frappe.db.delete("Model", {"name": self.model})
		frappe.db.delete("Model Provider", {"name": self.provider})
		seed.insert_missing(self.path)
		with patch.object(seed, "CATALOG", self.path):
			pricing = frappe.get_doc(
				"Model Pricing", frappe.get_doc("Model", {"model_key": key}).load_pricing()
			)
		pricing.status = "Enabled"
		pricing.save()
		self.assertEqual(self.exported(), first)


class TestTheShippedFile(unittest.TestCase):
	"""A typo here is seeded into every site on its next migrate."""

	def setUp(self):
		self.catalog = seed.read()

	def test_every_name_a_model_uses_is_defined_in_the_file(self):
		providers = {p["provider_name"] for p in self.catalog["providers"]}
		counters = {c["counter_name"] for c in self.catalog["counters"]}
		geographies = {g["name"] for g in self.catalog["geographies"]}
		self.assertIn(self.catalog["default_tier"], {t["name"] for t in self.catalog["tiers"]})
		scopes = {t["name"]: t.get("limit_scope") for t in self.catalog["tiers"]}
		self.assertEqual({name for name, scope in scopes.items() if scope not in seed.LIMIT_TABLES}, set())
		inputs = {m["name"] for m in self.catalog["modalities"] if m["is_input"]}
		outputs = {m["name"] for m in self.catalog["modalities"] if m["is_output"]}
		self.assertEqual([r for r in self.catalog["regions"] if r["geography"] not in geographies], [])
		keys = [seed.get_model_key(m) for m in self.catalog["models"]]
		self.assertEqual(len(keys), len(set(keys)), "a model key is listed twice")
		pairs = [(d["provider_name"], d["tool"]) for d in self.catalog["denied_tools"]]
		self.assertEqual(len(pairs), len(set(pairs)), "a denied tool is listed twice")
		self.assertEqual([pair for pair in pairs if not all(pair)], [])
		for group in self.catalog["model_groups"]:
			self.assertEqual([key for key in group["models"] if key not in keys], [], group["name"])
		for model in self.catalog["models"]:
			with self.subTest(seed.get_model_key(model)):
				self.assertIn(model["provider"], providers)
				self.assertEqual([word for word in model["input_modalities"] if word not in inputs], [])
				self.assertEqual([word for word in model["output_modalities"] if word not in outputs], [])
				self.assertEqual([r["counter"] for r in model["rates"] if r["counter"] not in counters], [])
				self.assertEqual([r for r in model["rates"] if r["rate"] < 0], [])

	def test_it_carries_no_secret_and_no_operator_state(self):
		text = seed.CATALOG.read_text()
		for key in ("api_key", "access_key_id", "published", "endpoint", "fleet_zone", "vpc_id"):
			self.assertNotIn(f'"{key}"', text)


class TestTheCatalogInsertsATierOnce(CatalogCase):
	def test_a_per_key_tier_is_inserted_with_its_rows_in_the_per_key_table(self):
		entry = {"name": "catalog-tier-key", "spend_limit": 10, "limit_scope": "Key"}
		self.load(tiers=[{**entry, "limits": [{"metric": "requests", "window": "1m", "value": 60}]}])
		doc = frappe.get_doc("Tier", "catalog-tier-key")
		self.assertEqual(([row.value for row in doc.key_limits], doc.limits), ([60], []))
		dear = our_model("catalog-key-dear").insert()
		named = [{"model_key": dear.model_key, "metric": "requests", "window": "1m", "value": 6}]
		with self.assertRaisesRegex(frappe.ValidationError, "its rows name no model"):
			self.load(tiers=[{**entry, "name": "catalog-tier-key-named", "limits": named}])

	def test_the_move_patch_leaves_a_tier_counting_per_key_and_the_file_writes_there(self):
		from grove.patches.v1_0.move_tier_limits_per_key import execute

		tiers = [
			{
				"name": "catalog-tier-moved",
				"spend_limit": 10,
				"limits": [{"metric": "requests", "window": "1m", "value": 60}],
			}
		]
		self.load(tiers=tiers)
		dear = our_model("catalog-moved-dear").insert()
		doc = frappe.get_doc("Tier", "catalog-tier-moved")
		doc.append("limits", {"model": dear.name, "metric": "requests", "window": "1m", "value": 6})
		doc.save()
		execute()
		doc.reload()
		self.assertEqual(
			(doc.limit_scope, [row.value for row in doc.key_limits], [row.model for row in doc.limits]),
			("Key", [60], [dear.name]),
		)
		self.load(tiers=tiers)
		self.assertEqual(frappe.db.get_value("Tier", "catalog-tier-moved", "modified"), doc.modified)

	def test_a_site_from_before_the_scope_settled_is_brought_in_line(self):
		from grove.patches.v1_0.settle_limit_scope_and_tables import execute

		dear = our_model("catalog-settled-dear").insert()
		rows = [
			{"metric": "requests", "window": "1m", "value": 60},
			{"model": dear.name, "metric": "requests", "window": "1m", "value": 6},
		]
		for name in ("catalog-tier-old-team", "catalog-tier-old-key"):
			frappe.get_doc({"doctype": "Tier", "__newname": name, "limits": rows}).insert()
		frappe.db.set_value("Tier", "catalog-tier-old-team", "limit_scope", "Team")
		frappe.db.set_value("Tier", "catalog-tier-old-key", "limit_scope", "Key")
		frappe.db.set_value("Model Limit", {"parent": "catalog-tier-old-key"}, "parentfield", "key_limits")
		execute()
		renamed, moved = (
			frappe.get_doc("Tier", name) for name in ("catalog-tier-old-team", "catalog-tier-old-key")
		)
		self.assertEqual((renamed.limit_scope, len(renamed.limits), renamed.key_limits), ("Model", 2, []))
		self.assertEqual(
			(moved.limit_scope, [row.value for row in moved.key_limits], [row.model for row in moved.limits]),
			("Key", [60], [dear.name]),
		)
		self.assertFalse(frappe.db.exists("Model Limit", {"parentfield": "key_limits"}))

	def test_a_tier_the_site_holds_is_left_as_the_operator_has_it(self):
		make_test_geography()
		dear = our_model("catalog-dear").insert()
		rows = [
			{"metric": "requests", "window": "1m", "value": 60},
			{"model_key": dear.model_key, "metric": "requests", "window": "1m", "value": 6},
		]
		self.load(tiers=[{"name": "catalog-tier", "spend_limit": 10, "limits": rows}])
		doc = frappe.get_doc("Tier", "catalog-tier")
		self.assertEqual(
			(doc.spend_limit, [(r.model, r.model_key, r.value) for r in doc.limits]),
			(10, [(None, None, 60), (dear.name, dear.model_key, 6)]),
			"the file names a model by key; the row links its doc",
		)
		doc.spend_limit, doc.limit_scope = 25, "Key"
		doc.set("limits", [{"metric": "requests", "window": "1m", "value": 70}])
		doc.set("key_limits", [{"metric": "requests", "window": "1m", "value": 7}])
		doc.save()
		self.load(tiers=[{"name": "catalog-tier", "spend_limit": 10, "limits": rows}])
		kept = frappe.get_doc("Tier", "catalog-tier")
		self.assertEqual(
			(
				kept.spend_limit,
				kept.limit_scope,
				[r.value for r in kept.limits],
				[r.value for r in kept.key_limits],
			),
			(25, "Key", [70], [7]),
		)
		self.assertEqual(str(kept.modified), str(doc.modified))
