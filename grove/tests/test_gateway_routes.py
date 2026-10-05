# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The table a GATEWAY is given: one row per model, naming ingresses and direct engines.

The point of the split is what is absent. A box behind an ingress contributes a row that names the
ingress and never its own address, so replica topology stays inside its VPC and a pod restarting in
one region is not an event a gateway in another ever sees. Several deployments behind one ingress
fold into ONE row — otherwise the gateway would hold a row per replica again, which is the N x M
table this whole design exists to remove.
"""

import unittest
from unittest.mock import patch

import frappe

from grove.tests.model_rows import modality_rows, model_row

ZONE = "grove.example.com"

MODELS = [model_row("qwen3-35b", input_modalities=["text", "image"]), model_row("llama-70b")]
INGRESSES = [
	{"name": "aps1-i1", "region": "ap-south-1", "status": "Active"},
	{"name": "aps1-i2", "region": "ap-south-1", "status": "Active"},
	{"name": "aps1-down", "region": "ap-south-1", "status": "Broken"},
]
SERVERS = [
	{"name": "INF-a", "ingress": "aps1-i1"},
	{"name": "INF-b", "ingress": "aps1-i1"},
	{"name": "INF-c", "ingress": "aps1-i2"},
	{"name": "INF-orphan", "ingress": "aps1-down"},
	{"name": "INF-direct", "ingress": None},
]
DEPLOYMENTS = [
	# Two boxes behind one ingress, same model: they must fold into a single row.
	{"name": "MD-1", "model": "qwen3-35b", "model_key": "qwen3-35b", "engine_url": "https://203.0.113.1/e/md-1",
	 "status": "Active", "inference_server": "INF-a", "max_num_seqs": 8},
	{"name": "MD-2", "model": "qwen3-35b", "model_key": "qwen3-35b", "engine_url": "https://203.0.113.2/e/md-2",
	 "status": "Active", "inference_server": "INF-b", "max_num_seqs": 4},
	# A second ingress, same model: its own row.
	{"name": "MD-3", "model": "qwen3-35b", "model_key": "qwen3-35b", "engine_url": "https://203.0.113.3/e/md-3",
	 "status": "Active", "inference_server": "INF-c", "max_num_seqs": 16},
	# No ingress: the direct path, exactly as before.
	{"name": "MD-4", "model": "llama-70b", "model_key": "llama-70b", "engine_url": "https://203.0.113.4/e/md-4",
	 "status": "Active", "inference_server": "INF-direct", "max_num_seqs": 4},
	# Behind an ingress that is not Active — no row for it at all.
	{"name": "MD-5", "model": "llama-70b", "model_key": "llama-70b", "engine_url": "https://203.0.113.5/e/md-5",
	 "status": "Active", "inference_server": "INF-orphan", "max_num_seqs": 4},
]
PODS = [{"name": "POD-1", "model": "llama-70b", "model_key": "llama-70b", "engine_url": "http://1.2.3.4:8081", "max_num_seqs": 2}]


class FakeQuery:
	def __init__(self, zone=ZONE):
		self.zone = zone

	def __call__(self, doctype, filters=None, fields=None, pluck=None, **kwargs):
		filters = dict(filters or {})
		rows = {
			"Model": MODELS,
			"Model Modality Row": modality_rows(MODELS),
			"Model Replica": DEPLOYMENTS,
			"Pod": PODS,
			"Ingress Server": INGRESSES,
			"Inference Server": SERVERS,
		}.get(doctype, [])
		if doctype == "Ingress Server" and filters.get("status"):
			rows = [r for r in rows if r["status"] == filters["status"]]
		if doctype == "Inference Server" and "ingress" in filters:
			rows = [r for r in rows if r["ingress"]]
		if pluck:
			return [r[pluck] for r in rows]
		return [frappe._dict(r) for r in rows]


def routes(zone=ZONE):
	from grove.pathway import routes

	with (
		patch.object(frappe, "get_all", side_effect=FakeQuery(zone)),
		patch("grove.pathway.routes.published_routes", side_effect=lambda table, models: table),
		patch.object(frappe, "db", frappe._dict(get_value=lambda *args: zone, get_single_value=lambda *args: "in")),
		patch.object(
			frappe, "get_doc",
			side_effect=lambda *a, **k: frappe._dict(get_password=lambda *a, **k: "secret"),
		),
	):
		return routes.gateway_routes("in")


def rows_for(model, zone=ZONE):
	return routes(zone)[model]


class TestIngressRows(unittest.TestCase):
	def test_boxes_behind_one_ingress_fold_into_one_row(self):
		ingress_rows = [r for r in rows_for("qwen3-35b") if r["kind"] == "ingress"]
		self.assertEqual(
			sorted(r["engine_url"] for r in ingress_rows),
			[f"https://aps1-i1.{ZONE}", f"https://aps1-i2.{ZONE}"],
		)

	def test_the_folded_capacity_is_the_sum_behind_that_ingress(self):
		# Advisory at this tier — it is how the gateway chooses BETWEEN ingresses. The exact
		# per-replica gate lives on the ingress, which is the tier that owns the counters.
		[row] = [r for r in rows_for("qwen3-35b") if r["engine_url"].startswith(f"https://aps1-i1.")]
		self.assertEqual(row["capacity"], 12)  # MD-1 8 + MD-2 4

	def test_every_row_says_what_its_model_takes_and_gives(self):
		# On every kind of row: the gateway gates the surface and checks a caller's fallbacks on it.
		for row in rows_for("qwen3-35b"):
			with self.subTest(row["engine_url"]):
				self.assertEqual((row["input_modalities"], row["output_modalities"]), (["text", "image"], ["text"]))
		for row in rows_for("llama-70b"):
			self.assertEqual((row["input_modalities"], row["output_modalities"]), (["text"], ["text"]))
			self.assertNotIn("modality", row)

	def test_an_ingress_row_names_no_replica(self):
		# The whole point: replica topology never leaves its VPC.
		for row in rows_for("qwen3-35b"):
			with self.subTest(row["engine_url"]):
				for address in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
					self.assertNotIn(address, str(row))

	def test_an_ingress_row_carries_its_region_for_tiering(self):
		[row] = [r for r in rows_for("qwen3-35b") if "aps1-i2" in r["engine_url"]]
		self.assertEqual(row["region"], "ap-south-1")

	def test_the_url_has_no_path_because_lua_appends_the_request_uri(self):
		for row in rows_for("qwen3-35b"):
			with self.subTest(row["engine_url"]):
				self.assertFalse(row["engine_url"].endswith("/"))
				self.assertNotIn("/e/", row["engine_url"])

	def test_an_ingress_that_is_not_active_is_not_routed_to(self):
		# Its boxes go dark rather than falling back to a direct dial: the gateway may not have a
		# route to a private box, and a public one would undo the split.
		self.assertFalse([r for r in rows_for("llama-70b") if "aps1-down" in r["engine_url"]])
		self.assertFalse([r for r in rows_for("llama-70b") if "203.0.113.5" in r["engine_url"]])


class TestDirectRowsAreUnchanged(unittest.TestCase):
	def test_a_box_with_no_ingress_is_still_dialled_directly(self):
		[row] = [r for r in rows_for("llama-70b") if r["deployment"] == "MD-4"]
		self.assertEqual(row["engine_url"], "https://203.0.113.4/e/md-4")
		self.assertEqual(row["kind"], "direct")

	def test_a_pod_is_always_direct(self):
		# A pod has no Machine and so no ingress; provider TLS already covers that hop.
		[row] = [r for r in rows_for("llama-70b") if r["deployment"] == "POD-1"]
		self.assertEqual(row["kind"], "direct")

	def test_with_no_fleet_zone_every_box_falls_back_to_direct(self):
		# An ingress has no name to be reached at until a zone is set, and a row with a blank URL
		# is unroutable — pickRoute drops it and the model reads down.
		for row in rows_for("qwen3-35b", zone=""):
			with self.subTest(row["deployment"]):
				self.assertEqual(row["kind"], "direct")
				self.assertTrue(row["engine_url"].startswith("https://203.0.113."))


if __name__ == "__main__":
	unittest.main()
