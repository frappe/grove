# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""The Ops page reads what the control API serves, fleet-wide and for System Managers alone: the
window's totals and trend, who spends, each key against its cap, what is stuck, each vendor key's
counters, and — on demand — the boxes' counters beside Grove's."""

import json
import unittest.mock
from datetime import timedelta
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from grove.billing.pricing import month_spent_by
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.model_provider.test_model_provider import provider, vendor_model
from grove.grove.page.ops import ops
from grove.pathway.run import finalize, new_run
from grove.utils import utc_today

VENDOR = "_T-ops-vendor"
MODEL_A = f"{VENDOR}/alpha"
MODEL_B = f"{VENDOR}/beta"
WEEK = {"period": "Last 7 Days"}


class TestOpsPage(IntegrationTestCase):
	"""One team, a live key and a revoked one; usage today, yesterday, ten and forty days ago."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
		cls.geography = make_test_geography()
		cls.other_geography = make_test_geography("t-ops-elsewhere")
		cls.vendor = provider(VENDOR, api_key="k").insert()
		cls.model_doc = vendor_model("alpha", cls.vendor.name).insert(ignore_permissions=True).name
		cls.tier = (
			frappe.get_doc(
				{
					"doctype": "Tier",
					"__newname": "_T-ops-tier",
					"max_keys": 5,
					"spend_limit": 100,
					"limits": [{"metric": "requests", "window": "1m", "value": 60}],
				}
			)
			.insert()
			.name
		)
		cls.team = (
			frappe.get_doc({"doctype": "Grove Team", "__newname": "_T-ops-team", "tier": cls.tier})
			.insert(ignore_permissions=True)
			.name
		)
		cls.key = cls.make_key("alpha", cap=10)
		cls.revoked = cls.make_key("beta", cap=0)
		frappe.db.set_value("Grove API Key", cls.revoked, "status", "revoked")

		cls.today = utc_today()
		cls.record(cls.key, cls.today, MODEL_A, cost=1.0, requests=2, gateway_cost=0.4)
		cls.record(cls.key, cls.today, MODEL_B, cost=0.5, requests=1, gateway_cost=0.1)
		cls.record(
			cls.revoked, cls.today - timedelta(days=1), MODEL_A, cost=2.0, requests=1, gateway_cost=0.8
		)
		cls.record(cls.key, cls.today - timedelta(days=10), MODEL_A, cost=3.0, requests=3, gateway_cost=1.0)
		cls.record(cls.key, cls.today - timedelta(days=40), MODEL_A, cost=7.0, requests=7, gateway_cost=2.0)

		cls.gateway = "_T-ops-gw"
		frappe.get_doc({"doctype": "Gateway Server", "name": cls.gateway}).db_insert()
		run = new_run("Usage", "Scheduled")
		run.append(
			"results",
			{
				"server_type": "Gateway Server",
				"server": cls.gateway,
				"reachable": 1,
				"success": 1,
				"duration_ms": 12,
			},
		)
		finalize(run, 1, 1)
		frappe.get_doc(
			{
				"doctype": "Provider Key Stats",
				"model_provider": cls.vendor.name,
				"key_title": cls.vendor.api_keys[0].title,
				"key_id": cls.vendor.api_keys[0].name,
				"stats": json.dumps(
					{"requests": 10, "ok": 7, "rate_limited": 1, "unauthorized": 2, "unpaid": 0}
				),
			}
		).insert(ignore_permissions=True)

	@classmethod
	def make_key(cls, title, cap):
		return (
			frappe.get_doc({"doctype": "Grove API Key", "team": cls.team, "title": title, "cap": cap})
			.insert(ignore_permissions=True)
			.name
		)

	@classmethod
	def record(cls, key, day, model, cost, requests, gateway_cost):
		entry = {
			"model": model,
			"pricing": None,
			"requests": requests,
			"completion_tokens": 100,
			"grove_cost": cost,
		}
		frappe.get_doc(
			{
				"doctype": "Usage Record",
				"api_key": key,
				"team": cls.team,
				"day": day,
				"drain_id": f"ops-{key}-{day}-{model}",
				"request_count": requests,
				"cost": cost,
				"gateway_cost": gateway_cost,
				"usage": frappe.as_json([entry]),
			}
		).insert(ignore_permissions=True)

	def filters(self, **more):
		return {**WEEK, "team": self.team, **more}

	def test_overview_sums_the_window_beside_the_one_before(self):
		answer = ops.overview(self.filters())
		totals = answer["totals"]
		self.assertEqual(
			(totals["requests"], totals["tokens"], totals["cost"], totals["teams"], totals["keys"]),
			(4, 300, 3.5, 1, 2),
		)
		self.assertAlmostEqual(totals["gateway_cost"], 1.3)
		self.assertAlmostEqual(totals["drift"], 2.2)
		self.assertEqual(answer["previous"]["cost"], 3.0)

	def test_a_model_filter_narrows_cost_and_drops_the_box_side(self):
		totals = ops.overview(self.filters(model=self.model_doc))["totals"]
		self.assertEqual((totals["cost"], totals["requests"]), (3.0, 3))
		self.assertIsNone(totals["gateway_cost"])
		self.assertIsNone(totals["drift"])

	def test_trend_folds_models_into_their_provider(self):
		by_provider = ops.trend(self.filters(), "provider")
		self.assertEqual({row["label"] for row in by_provider}, {VENDOR})
		self.assertEqual(sum(row["cost"] for row in by_provider), 3.5)
		today = [row for row in ops.trend(self.filters(), "model") if row["day"] == str(self.today)]
		self.assertEqual({row["label"]: row["cost"] for row in today}, {MODEL_A: 1.0, MODEL_B: 0.5})
		with self.assertRaises(frappe.ValidationError):
			ops.trend(self.filters(), "colour")

	def test_leaders_rank_by_cost_and_name_the_key(self):
		leaders = ops.leaders(self.filters())
		self.assertEqual([row["name"] for row in leaders["keys"]], [self.revoked, self.key])
		self.assertEqual((leaders["keys"][0]["title"], leaders["keys"][0]["team"]), ("beta", self.team))
		self.assertEqual(
			(leaders["models"][0]["name"], leaders["models"][0]["model_doc"]), (MODEL_A, self.model_doc)
		)
		self.assertEqual(leaders["teams"], [{"name": self.team, "requests": 4, "tokens": 300, "cost": 3.5}])

	def test_teams_carry_the_tier_and_the_month(self):
		row = next(row for row in ops.teams(self.filters()) if row["team"] == self.team)
		expected = float(month_spent_by("team", [self.team]).get(self.team, 0))
		self.assertEqual(
			(
				row["tier"],
				row["keys_live"],
				row["max_keys"],
				row["spend_limit"],
				row["last_used"],
				row["cost"],
			),
			(self.tier, 1, 5, 100.0, str(self.today), 3.5),
		)
		self.assertEqual(row["month_spent"], expected)
		self.assertGreaterEqual(row["committed"], 10.0)

	def test_keys_read_cap_and_last_use(self):
		rows = {row["name"]: row for row in ops.keys(self.filters())}
		live, revoked = rows[self.key], rows[self.revoked]
		self.assertEqual(
			(live["status"], live["last_used"], live["team"]), ("active", str(self.today), self.team)
		)
		self.assertEqual(live["cap_pct"], round(live["month_spent"] / 10 * 100, 1))
		self.assertEqual(live["limits"], [{"metric": "requests", "window": "1m", "value": 60}])
		self.assertEqual((revoked["status"], revoked["cap_pct"]), ("revoked", None))
		self.assertEqual([row["status"] for row in ops.keys(self.filters())], ["active", "revoked"])
		self.assertEqual(ops.keys(self.filters(geography=self.other_geography)), [])
		self.assertNotIn("key_hash", live)

	def test_gateways_carry_their_last_pull(self):
		answer = ops.gateways()
		box = next(box for box in answer["gateways"] if box["name"] == self.gateway)
		self.assertEqual((box["pull"]["success"], box["pull"]["duration_ms"]), (1, 12))
		self.assertIsNotNone(box["pull"]["run_at"])
		self.assertIsNone(box["push"])
		self.assertIn("projection", answer["paused"])

	def test_key_stats_sum_the_window(self):
		answer = ops.key_stats({"period": "Today", "provider": VENDOR})
		[row] = answer["rows"]
		self.assertEqual((row["requests"], row["unauthorized"], row["key_title"]), (10, 2, "key"))
		self.assertIsNotNone(answer["last_pull"])
		self.assertEqual(ops.key_stats({"period": "Last Month", "provider": VENDOR})["rows"], [])

	def test_peek_reads_the_boxes_beside_grove(self):
		grove = month_spent_by("api_key", [self.key, self.revoked])
		box = {self.key: grove[self.key], self.revoked: grove.get(self.revoked, Decimal(0)) + Decimal("0.25")}
		with unittest.mock.patch("grove.pathway.usage.peek_team", return_value=box):
			answer = ops.peek(self.team)
		rows = {row["key"]: row for row in answer["rows"]}
		self.assertEqual(rows[self.key]["verdict"], "in step")
		self.assertEqual(rows[self.revoked]["delta"], 0.25)
		self.assertIn("next pull", rows[self.revoked]["verdict"])
		with unittest.mock.patch(
			"grove.pathway.usage.peek_team", side_effect=frappe.ValidationError("paused")
		):
			with self.assertRaises(frappe.ValidationError):
				ops.peek(self.team)

	def test_filter_options_list_what_the_row_offers(self):
		options = ops.filter_options(team=self.team)
		self.assertIn(self.team, options["teams"])
		self.assertEqual({key["name"] for key in options["keys"]}, {self.key, self.revoked})
		self.assertIn((MODEL_A, self.model_doc), {(m["model_key"], m["name"]) for m in options["models"]})
		self.assertIn(VENDOR, options["providers"])
		self.assertEqual(ops.filter_options()["keys"], [])

	def test_only_a_system_manager_reads(self):
		self.addCleanup(frappe.set_user, "Administrator")
		control = self.user("_t-ops-control@example.com", "Grove Control", "Website User")
		frappe.set_user(control)
		with self.assertRaises(frappe.PermissionError):
			ops.overview(WEEK)
		frappe.set_user(self.user("_t-ops-manager@example.com", "System Manager", "System User"))
		self.assertIn("totals", ops.overview(WEEK))

	def user(self, email, role, user_type):
		if frappe.db.exists("User", email):
			return email
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Ops",
					"user_type": user_type,
					"roles": [{"role": role}],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
