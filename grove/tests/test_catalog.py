# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The catalog inserts what a site lacks and leaves what it holds; its export carries no secret
and loads back to the same file."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from grove.catalog import export, seed
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_pricing.test_model_pricing import enabled_pricing
from grove.grove.doctype.model_provider.test_model_provider import our_model, provider, vendor_model

COUNTED = ("Usage Counter", "Geography", "Model Provider", "Model", "Model Pricing")


class CatalogCase(IntegrationTestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.path = Path(directory.name) / "catalog.json"

	def load(self, **catalog):
		self.path.write_text(json.dumps({"geographies": [], "providers": [], "models": []} | catalog))
		seed.insert_missing(self.path)


class TestInsertMissing(CatalogCase):
	CATALOG = {
		"counters": [{"counter_name": "web_search_requests", "label": "Web searches", "unit": "request"}],
		"geographies": [{"name": "Catalog"}],
		"providers": [{"provider_name": "catalog-vendor", "geography": "Catalog", "base_url": "https://api.vendor.test/v1"}],
		"models": [{
			"provider": "catalog-vendor", "model_id": "big-1", "upstream_model_id": "big-1-2026", "modality": "text",
			"rates": [{"counter": "prompt_tokens", "rate": 2.5}],
		}],
	}

	def test_what_is_absent_is_inserted_once_and_never_its_pricing(self):
		make_test_geography()
		default = frappe.db.get_value("Geography", {"is_default": 1})
		self.load(**self.CATALOG)
		geography = frappe.get_doc("Geography", "Catalog")
		self.assertFalse(geography.endpoint or geography.fleet_zone or geography.is_default)
		self.assertEqual(frappe.db.get_value("Geography", {"is_default": 1}), default)
		model = frappe.get_doc("Model", {"model_key": "catalog-vendor/big-1"})
		self.assertEqual((model.upstream_model_id, model.published, model.provider_is_self_hosted), ("big-1-2026", 0, 0))
		self.assertFalse(frappe.db.exists("Model Pricing", {"model": model.name}))
		self.assertEqual(frappe.db.get_value("Usage Counter", "web_search_requests", "unit"), "request")
		counts = [frappe.db.count(doctype) for doctype in COUNTED]
		self.load(**self.CATALOG)
		self.assertEqual([frappe.db.count(doctype) for doctype in COUNTED], counts)

	def test_a_provider_the_site_holds_is_left_as_it_is(self):
		held = provider("catalog-held", base_url="https://held.test/v1", api_key="held-secret").insert()
		self.load(providers=[{"provider_name": "catalog-held", "geography": "Catalog", "base_url": "https://new.test/v1"}])
		[name] = frappe.get_all("Model Provider", filters={"provider_name": "catalog-held"}, pluck="name")
		doc = frappe.get_doc("Model Provider", name)
		self.assertEqual(
			(doc.name, doc.base_url, doc.geography, doc.api_keys[0].get_password("api_key")),
			(held.name, "https://held.test/v1", held.geography, "held-secret"),
		)

	def test_a_model_under_a_provider_nobody_carries_throws(self):
		with self.assertRaises(frappe.ValidationError):
			self.load(models=[{"provider": "catalog-nobody", "model_id": "big-1", "modality": "text"}])


class TestASiteWithNoDefaultGeography(CatalogCase):
	"""Its own class: the rollback is once per class, and this one moves the default."""

	def test_the_first_geography_inserted_is_the_default(self):
		frappe.db.set_value("Geography", {"is_default": 1}, "is_default", 0)
		self.load(geographies=[{"name": "Catalog-First"}, {"name": "Catalog-Second"}])
		self.assertEqual(frappe.get_all("Geography", filters={"is_default": 1}, pluck="name"), ["Catalog-First"])


class TestExport(CatalogCase):
	RATES = {"prompt_tokens": 2.5, "completion_tokens": 15, "prompt_tokens_above_272k": 5}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		vendor = provider("catalog-export", base_url="https://export.test/v1", api_key="export-secret").insert()
		cls.provider = vendor.name
		cls.model = vendor_model("big-2", vendor.name).insert().name
		cls.pricing = enabled_pricing(cls.model, **cls.RATES).name
		cls.ours = our_model("catalog-ours-7b").insert().name

	def exported(self):
		export.write(self.path)
		return self.path.read_text()

	@staticmethod
	def key(model):
		return frappe.db.get_value("Model", model, "model_key")

	def test_no_secret_no_model_of_ours_and_no_geography_but_main(self):
		text = self.exported()
		catalog = json.loads(text)
		self.assertNotIn("api_key", text)
		self.assertNotIn("export-secret", text)
		self.assertNotIn("published", text)
		self.assertEqual(catalog["geographies"], [{"name": "Main"}])
		# The shipped file is the site's table: roots with a unit first, derived rows after their base.
		self.assertEqual(catalog["counters"], seed.read()["counters"])
		self.assertEqual({p.get("geography") for p in catalog["providers"] if not p.get("is_self_hosted")}, {"Main"})
		[model] = [m for m in catalog["models"] if seed.get_model_name(m) == self.key(self.model)]
		self.assertEqual(model["rates"], [{"counter": c, "rate": r} for c, r in self.RATES.items()])
		self.assertNotIn(self.key(self.ours), [seed.get_model_name(m) for m in catalog["models"]])

	def test_the_file_loaded_where_its_entries_are_missing_exports_the_same(self):
		first = self.exported()
		key = self.key(self.model)
		frappe.db.delete("Model Pricing Rate", {"parent": self.pricing})
		frappe.db.delete("Model Pricing", {"name": self.pricing})
		frappe.db.delete("Model", {"name": self.model})
		frappe.db.delete("Model Provider", {"name": self.provider})
		seed.insert_missing(self.path)
		# Reseeded under a new hash: the key is what came back, not the doc name.
		with patch.object(seed, "CATALOG", self.path):
			pricing = frappe.get_doc("Model Pricing", frappe.get_doc("Model", {"model_key": key}).load_pricing())
		pricing.status = "Enabled"
		pricing.save()
		self.assertEqual(self.exported(), first)

