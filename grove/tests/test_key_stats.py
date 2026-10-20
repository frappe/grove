# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The 6-hourly pull lands one Provider Key Stats row per key a store has dialled, as the store
counted it, and never touches a row again."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from grove.grove.doctype.model_provider.test_model_provider import provider
from grove.pathway.key_stats import KeyStats, held_keys
from grove.pathway.run import Unit, new_run


class TestKeyStatsPull(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.vendor = provider("probe-stats-vendor", base_url="https://api.probe.test", api_key="k").insert()
		cls.key = cls.vendor.api_keys[0].name
		for store in ("store-a", "store-b"):
			frappe.get_doc({"doctype": "Gateway Store", "name": store}).db_insert()

	def setUp(self):
		super().setUp()
		frappe.db.delete("Provider Key Stats", {"model_provider": self.vendor.name})

	def settle(self, stats, store="store-a"):
		"""One store's answer, landed as the run would land it."""
		run = KeyStats(trigger="Manual")
		run.doc = new_run(run.sync_type, run.trigger)
		run.keys = held_keys()
		row = {"server_type": "Gateway Server", "server": "gw-a", "success": 1, "stats": stats}
		return run.settle(Unit(store, ()), (True, [row]))

	def rows(self):
		rows = frappe.get_all(
			"Provider Key Stats",
			filters={"model_provider": self.vendor.name},
			order_by="creation asc",
			fields=["gateway_store", "stats", "key_title", "model_provider"],
		)
		for row in rows:
			row.stats = json.loads(row.stats)
		return rows

	def test_a_dialled_key_lands_as_the_store_counted_it(self):
		outcome, rows = self.settle(
			{
				self.key: {
					"requests": "3",
					"ok": "2",
					"unpaid": "1",
					"rejected": "1",
					"last_used": "1700000000",
				},
				"nobody": {"requests": "9"},
			}
		)
		self.assertTrue(outcome)
		self.assertEqual((rows[0]["had_data"], rows[0]["detail"]), (1, "keys:1"))
		self.assertNotIn("stats", rows[0])
		[row] = self.rows()
		self.assertEqual(
			(row.gateway_store, row.stats["requests"], row.stats["unpaid"], row.stats["rejected"]),
			("store-a", 3, 1, 1),
		)
		self.assertEqual((row.key_title, row.model_provider), ("key", self.vendor.name))
		self.assertEqual(row.stats["forbidden"], 0)
		self.assertRegex(row.stats["last_used"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
		self.assertIsNone(row.stats["last_rate_limited"])

	def test_a_store_that_never_dialled_the_key_leaves_no_row(self):
		outcome, rows = self.settle({self.key: {"requests": "0", "last_used": "0"}}, store="store-b")
		self.assertTrue(outcome)
		self.assertEqual((rows[0]["had_data"], rows[0]["detail"]), (0, "keys:0"))
		self.assertEqual([r.gateway_store for r in self.rows()], [])

	def test_every_pull_is_its_own_row(self):
		self.settle({self.key: {"requests": "1"}})
		self.settle({self.key: {"requests": "2"}})
		self.assertEqual([r.stats["requests"] for r in self.rows()], [1, 2])
