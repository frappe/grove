# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Billing end to end on a site: a drain is billed at Grove's price of the pricing the gateway
charged, the gateway's own charge is audited beside it, a discrepancy is corrected on the side
that is wrong, and the push carries each key its monthly cap."""

import unittest.mock
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from grove.api import usage as usage_api
from grove.billing import pricing
from grove.billing.doctype.credit_discrepancy.credit_discrepancy import mark_corrected, pending_adjustments
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.billing.doctype.model_pricing.test_model_pricing import enabled_pricing, new_pricing
from grove.billing.report.revenue.revenue import execute as revenue
from grove.pathway import routes, snapshot, usage
from grove.pathway.run import Target
from grove.tests.test_usage_pull import a_store

NANO = pricing.NANO
D = Decimal


class CreditsCase(IntegrationTestCase):
	"""A model sold at 10 USD/Mtok of completion, so 100 000 tokens cost exactly 1 USD."""

	RATE = 10
	counter = 0

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.enterClassContext(unittest.mock.patch("grove.pathway.usage.peek_team", return_value={}))
		make_test_geography()
		cls.model, cls.pricing = cls.priced_model("credits-7b", cls.RATE)
		cls.model_key = frappe.db.get_value("Model", cls.model, "model_key")
		cls.store = a_store("credits-store")
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"):
			machine = frappe.get_doc(
				{"doctype": "Machine", "name": "credits-box", "machine_type": "Gateway"}
			).insert(ignore_permissions=True)
			cls.gateway = (
				frappe.get_doc(
					{
						"doctype": "Gateway Server",
						"name": machine.name,
						"machine": machine.name,
						"gateway_store": cls.store,
						"is_store_writer": 1,
					}
				)
				.insert(ignore_permissions=True, ignore_mandatory=True)
				.name
			)
		frappe.db.set_value("Gateway Server", cls.gateway, "status", "Active")

	@classmethod
	def priced_model(cls, model_id, rate):
		model = (
			frappe.get_doc({"doctype": "Model", "model_id": model_id, "hf_repo": f"org/{model_id}"})
			.insert(ignore_permissions=True)
			.name
		)
		return model, enabled_pricing(model, completion_tokens=rate).name

	def team(self, cap=1, free=0):
		"""A team on a tier with a spend limit of 100 a month, with one key capped at `cap`."""
		CreditsCase.counter += 1
		if not frappe.db.exists("Tier", "credits-tier"):
			frappe.get_doc({"doctype": "Tier", "__newname": "credits-tier", "spend_limit": 100}).insert()
		doc = frappe.get_doc(
			{
				"doctype": "Central Team",
				"__newname": f"credits-{CreditsCase.counter}",
				"free": free,
				"tier": "credits-tier",
			}
		).insert(ignore_permissions=True)
		return doc.name, self.key(doc.name, cap)

	def key(self, team, cap=0):
		return (
			frappe.get_doc({"doctype": "Grove API Key", "team": team, "cap": cap})
			.insert(ignore_permissions=True)
			.name
		)

	def hash(self, tokens, cost=None, pricing_id=None, requests=1, charged=True):
		"""What one key's drained hash carries: the counters, tagged with the pricing the gateway
		priced at — `p:` charged, `f:` served free — and its cost. Values are strings, as Redis
		returns them."""
		p = f"{'p' if charged else 'f'}:{pricing_id or self.pricing}:"
		cost = tokens * self.RATE * 1000 if cost is None else cost
		h = {
			"completion_tokens": tokens,
			"request_count": requests,
			f"m:completion_tokens:{self.model_key}": tokens,
			f"{p}completion_tokens": tokens,
			f"{p}request_count": requests,
			f"{p}cost": cost,
			"cost": cost,
		}
		return {k: str(v) for k, v in h.items()}

	def pull(self, usages, store=None, drain_id=None):
		"""`usages`: {key: hash}, each served today."""
		CreditsCase.counter += 1
		drain_id = drain_id or f"drain-{CreditsCase.counter}"
		drains = {drain_id: {key: {str(usage.utc_today()): h} for key, h in usages.items()}}
		with unittest.mock.patch.object(frappe.db, "commit"):
			return usage.record_drains(self.gateway, drains, gateway_store=store or self.store)

	def state(self, team):
		row = frappe.db.get_value("Central Team", team, ["spent", "suspended"], as_dict=True)
		return D(str(row.spent)), row.suspended

	def discrepancies(self, team):
		return frappe.get_all(
			"Credit Discrepancy",
			filters={"team": team, "resolution": ("is", "not set")},
			fields=["name", "usage_record", "pricing", "gateway_value", "grove_value", "delta"],
		)

	def money(self, team):
		"""What the usage API, the Revenue report and `spend` say the team spent today."""
		today = str(usage.utc_today())
		_, rows = revenue({"from_date": today, "to_date": today, "group_by": "Team", "team": team})
		return (
			usage_api.usage([team], period="Today")[team]["cost"],
			sum(row["revenue"] for row in rows),
			usage_api.figures(team)["spend"]["month_spent"],
		)


class TestADrainIsBilledAtGrovesPrice(CreditsCase):
	def test_a_drain_bills_grove_price_with_both_costs_on_the_record(self):
		team, key = self.team(cap=1)
		self.pull({key: self.hash(50_000)})
		self.assertEqual(self.state(team), (D("0.5"), 0))
		record = frappe.get_doc("Usage Record", {"api_key": key})
		self.assertEqual((record.cost, record.gateway_cost, record.gateway_store), (0.5, 0.5, self.store))
		[entry] = frappe.parse_json(record.usage)
		self.assertEqual(
			(
				entry["pricing"],
				entry["model"],
				entry["requests"],
				entry["completion_tokens"],
				entry["grove_cost"],
			),
			(self.pricing, self.model_key, 1, 50_000, 0.5),
		)
		self.assertNotIn("gateway_cost", entry)
		self.assertEqual(self.discrepancies(team), [])

	def test_a_resent_drain_bills_nothing_twice(self):
		team, key = self.team(cap=1)
		self.pull({key: self.hash(50_000)}, drain_id="credits-resent")
		self.pull({key: self.hash(50_000)}, drain_id="credits-resent")
		self.assertEqual(self.state(team), (D("0.5"), 0))

	def test_each_request_is_billed_at_the_pricing_the_gateway_charged(self):
		_model, cheaper = self.priced_model("credits-switch-7b", 5)
		team, key = self.team(cap=5)
		h = self.hash(100_000)
		h.update(
			{
				k: v
				for k, v in self.hash(100_000, cost=500_000_000, pricing_id=cheaper).items()
				if k.startswith("p:")
			}
		)
		self.pull({key: h})
		self.assertEqual(self.state(team), (D("1.5"), 0))
		self.assertEqual(self.discrepancies(team), [])

	def above_272k_hash(self, model, pricing_id, cost):
		"""Two requests: 100 000 completion tokens on a prompt under 272k, 50 000 on one above."""
		p = f"p:{pricing_id}:"
		h = {"request_count": 2, f"{p}request_count": 2, f"{p}cost": cost, "cost": cost}
		for counter, tokens in {"completion_tokens": 100_000, "completion_tokens_above_272k": 50_000}.items():
			h |= {counter: tokens, f"m:{counter}:{model}": tokens, f"{p}{counter}": tokens}
		return {k: str(v) for k, v in h.items()}

	def test_tokens_above_272k_are_billed_at_their_own_rate_in_the_same_entry(self):
		model, _base = self.priced_model("credits-above-272k-7b", self.RATE)
		pricing_id = enabled_pricing(model, completion_tokens=10, completion_tokens_above_272k=20).name
		team, key = self.team(cap=5)
		self.pull({key: self.above_272k_hash(model, pricing_id, cost=2 * NANO)})
		self.assertEqual(self.state(team), (D("2"), 0))
		[entry] = frappe.parse_json(frappe.db.get_value("Usage Record", {"api_key": key}, "usage"))
		self.assertEqual(
			(
				entry["pricing"],
				entry["completion_tokens"],
				entry["completion_tokens_above_272k"],
				entry["grove_cost"],
			),
			(pricing_id, 100_000, 50_000, 2),
		)
		self.assertEqual(self.discrepancies(team), [])

	def test_tokens_above_272k_are_billed_at_the_base_rate_when_the_pricing_has_no_other(self):
		model, pricing_id = self.priced_model("credits-base-rate-7b", self.RATE)
		team, key = self.team(cap=5)
		self.pull({key: self.above_272k_hash(model, pricing_id, cost=1_500_000_000)})
		self.assertEqual(self.state(team), (D("1.5"), 0))
		[entry] = frappe.parse_json(frappe.db.get_value("Usage Record", {"api_key": key}, "usage"))
		self.assertEqual((entry["completion_tokens_above_272k"], entry["grove_cost"]), (50_000, 1.5))
		self.assertEqual(self.discrepancies(team), [])

	def test_spent_keeps_every_nano(self):
		"""Spent is compared with the gateway's own counter, so it holds what the records hold."""
		_model, pricing_id = self.priced_model("credits-nano-7b", 0.001)
		team, key = self.team(cap=1)
		for _ in range(2):
			self.pull({key: self.hash(1500, cost=1500, pricing_id=pricing_id)})
		self.assertEqual(self.state(team), (D("0.000003"), 0))
		self.assertEqual(self.discrepancies(team), [])

	def test_spending_past_the_cap_is_billed_and_grove_gates_nothing(self):
		team, key = self.team(cap=1)
		self.pull({key: self.hash(150_000)})
		self.assertEqual(self.state(team), (D("1.5"), 0))
		self.assertEqual(self.keys_pushed()[key]["limited"], False)

	def keys_pushed(self):
		section = snapshot.keys_section()
		return {k["prefix"]: k for bucket in section["buckets"].values() for k in bucket["records"]}


class TestAFreeTeamIsNeverCharged(CreditsCase):
	def record(self, key):
		return frappe.db.get_value(
			"Usage Record", {"api_key": key}, ["billed", "cost", "gateway_cost"], as_dict=True
		)

	def test_their_usage_is_recorded_with_its_cost_but_spent_does_not_move(self):
		free, key = self.team(cap=0, free=1)
		self.pull({key: self.hash(900_000, charged=False)})
		self.assertEqual(self.state(free), (D("0"), 0))
		self.assertEqual(self.record(key), {"billed": 0, "cost": 9, "gateway_cost": 9})

	def test_a_paying_teams_record_is_billed(self):
		_team, key = self.team(cap=1)
		self.pull({key: self.hash(50_000)})
		self.assertEqual(self.record(key).billed, 1)

	def test_turning_them_paid_starts_every_key_at_0_and_bills_what_follows(self):
		team, key = self.team(free=1, cap=5)
		self.pull({key: self.hash(900_000, charged=False)})
		doc = frappe.get_doc("Central Team", team)
		doc.free = 0
		doc.save()
		self.assertEqual(
			(self.state(team), frappe.db.get_value("Grove API Key", key, "cap")), ((D("0"), 0), 0)
		)
		self.pull({key: self.hash(30_000)})
		self.assertEqual(self.state(team), (D("0.3"), 0))

	def test_a_different_gateway_charge_is_no_discrepancy_when_nothing_was_billed(self):
		free, key = self.team(cap=0, free=1)
		self.pull({key: self.hash(50_000, cost=600_000_000, charged=False)})
		self.assertEqual(self.discrepancies(free), [])

	def test_the_usage_api_counts_their_requests_and_no_cost(self):
		free, key = self.team(cap=0, free=1)
		self.pull({key: self.hash(80_000, charged=False)})
		result = usage_api.usage([free], period="Today")
		self.assertEqual(result[free], {"requests": 1, "tokens": 80_000, "cost": 0})

	def test_untagged_usage_of_a_known_model_is_recorded_and_bills_nothing(self):
		team, key = self.team(cap=1)
		self.pull(
			{
				key: {
					"completion_tokens": "100000",
					"request_count": "1",
					f"m:completion_tokens:{self.model_key}": "100000",
				}
			}
		)
		self.assertEqual(self.state(team), (D("0"), 0))
		[entry] = frappe.parse_json(frappe.db.get_value("Usage Record", {"api_key": key}, "usage"))
		self.assertEqual(
			(entry["pricing"], entry["completion_tokens"], entry["grove_cost"]), (None, 100_000, 0)
		)

	def test_a_drain_that_spans_a_flip_lands_as_a_billed_and_a_free_record(self):
		team, key = self.team(cap=1)
		self.pull({key: self.hash(30_000) | self.hash(50_000, charged=False) | {"request_count": "2"}})
		self.assertEqual(self.state(team), (D("0.3"), 0), "only what the gateway charged moves spent")
		records = frappe.get_all(
			"Usage Record",
			{"api_key": key},
			["billed", "cost", "request_count"],
			order_by="billed desc",
			as_list=True,
		)
		self.assertEqual(records, ((1, 0.3, 1), (0, 0.5, 1)))

	def test_billing_follows_the_gateways_tag_not_the_teams_flag_at_the_pull(self):
		turned_free, charged_key = self.team(cap=1, free=1)
		self.pull({charged_key: self.hash(20_000)})
		self.assertEqual((self.state(turned_free)[0], self.record(charged_key).billed), (D("0.2"), 1))
		turned_prepaid, free_key = self.team(cap=1)
		self.pull({free_key: self.hash(20_000, charged=False)})
		self.assertEqual((self.state(turned_prepaid)[0], self.record(free_key).billed), (D("0"), 0))

	def test_an_unknown_pricing_leaves_the_teams_share_stuck_and_unacknowledged(self):
		team, key = self.team(cap=1)
		self.assertEqual(
			self.pull({key: self.hash(10_000, pricing_id="no-such-pricing")}, drain_id="credits-unknown"),
			(1, {}, [], 1),
		)
		self.assertEqual(self.state(team), (D("0"), 0))
		self.assertTrue(frappe.db.exists("Stuck Usage", {"team": team, "resolved": 0}))

	def test_a_stale_form_keeps_the_databases_spent(self):
		team, key = self.team(cap=1)
		doc = frappe.get_doc("Central Team", team)
		self.pull({key: self.hash(70_000)})
		doc.log_payloads = 1
		doc.save()
		self.assertEqual(self.state(team), (D("0.7"), 0))


class TestTheUsageApiSumsInTheDatabase(CreditsCase):
	def test_requests_and_cost_per_team_and_model_over_a_period(self):
		team, key = self.team(cap=5)
		self.pull({key: self.hash(50_000)})
		self.pull({key: self.hash(30_000, requests=2)})
		result = usage_api.usage([team], period="Today")
		self.assertEqual(result[team], {"requests": 3, "tokens": 80_000, "cost": 0.8})
		self.assertEqual(
			result["model_summary"], [{"model": self.model_key, "requests": 3, "tokens": 80_000, "cost": 0.8}]
		)
		self.assertEqual(
			result["daily_summary"],
			[
				{
					"day": result["to_date"],
					"model": self.model_key,
					"requests": 3,
					"tokens": 80_000,
					"cost": 0.8,
				}
			],
		)
		self.assertTrue(result["as_of"].endswith("Z"))
		self.assertEqual(usage_api.usage([team], period="Yesterday")[team], {"requests": 0, "tokens": 0, "cost": 0})

	def test_a_key_hash_narrows_it_to_that_key(self):
		team, key = self.team(cap=3)
		other_key = self.key(team, 2)
		self.pull({key: self.hash(50_000), other_key: self.hash(30_000, requests=2)})
		key_hash = frappe.db.get_value("Grove API Key", key, "key_hash")

		result = usage_api.usage([team], period="Today", key_hash=key_hash)
		self.assertEqual(result[team], {"requests": 1, "tokens": 50_000, "cost": 0.5})
		self.assertEqual(
			result["model_summary"], [{"model": self.model_key, "requests": 1, "tokens": 50_000, "cost": 0.5}]
		)
		self.assertEqual(
			result["daily_summary"],
			[
				{
					"day": result["to_date"],
					"model": self.model_key,
					"requests": 1,
					"tokens": 50_000,
					"cost": 0.5,
				}
			],
		)

	def test_another_teams_key_is_refused(self):
		team, _ = self.team(cap=5)
		_, stranger_key = self.team(cap=5)
		key_hash = frappe.db.get_value("Grove API Key", stranger_key, "key_hash")

		with self.assertRaises(frappe.DoesNotExistError):
			usage_api.usage([team], period="Today", key_hash=key_hash)


class TestTheGatewaysChargeIsAudited(CreditsCase):
	def test_a_different_charge_is_one_discrepancy_on_its_record_and_grove_bills_its_own_price(self):
		team, key = self.team(cap=5)
		self.pull({key: self.hash(50_000, cost=600_000_000)})
		[row] = self.discrepancies(team)
		record = frappe.db.get_value("Usage Record", {"api_key": key})
		self.assertEqual(
			(
				row.usage_record,
				row.pricing,
				D(str(row.gateway_value)),
				D(str(row.grove_value)),
				D(str(row.delta)),
			),
			(record, self.pricing, D("0.6"), D("0.5"), D("0.1")),
		)
		self.assertEqual(self.state(team), (D("0.5"), 0))

	def test_truncation_up_to_eight_nano_a_request_is_not_a_discrepancy(self):
		team, key = self.team(cap=5)
		self.pull({key: self.hash(50_000, cost=500_000_000 - 16, requests=2)})
		self.assertEqual(self.discrepancies(team), [])
		self.pull({key: self.hash(50_000, cost=500_000_000 - 17, requests=2)})
		self.assertEqual(len(self.discrepancies(team)), 1)


class TestADiscrepancyIsCorrectedOnTheWrongSide(CreditsCase):
	def discrepancy(self):
		team, key = self.team(cap=5)
		self.pull({key: self.hash(50_000, cost=600_000_000)})
		[row] = self.discrepancies(team)
		return team, frappe.get_doc("Credit Discrepancy", row.name)

	def test_grove_is_wrong_moves_spent_by_the_delta_and_settles(self):
		team, row = self.discrepancy()
		self.assertEqual(self.money(team), (0.5, 0.5, 0.5))
		row.correct_grove()
		row.reload()
		self.assertEqual(self.state(team)[0], D("0.6"))
		self.assertEqual((row.resolution, D(str(row.correction))), ("Grove corrected", D("0.1")))
		self.assertEqual(row.resolved_day, usage.utc_today())
		self.assertEqual(self.money(team), (0.6, 0.6, 0.6))
		self.assertEqual(D(str(frappe.db.get_value("Usage Record", row.usage_record, "cost"))), D("0.5"))
		# A row from before pricings were tagged: its money still counts, under no model.
		frappe.db.set_value("Credit Discrepancy", row.name, "pricing", None)
		self.assertEqual(self.money(team), (0.6, 0.6, 0.6))
		self.assertIn("", [entry["model"] for entry in usage_api.usage([team], period="Today")["model_summary"]])
		with self.assertRaises(frappe.ValidationError):
			row.correct_grove()

	def test_gateway_is_wrong_marks_the_row_pending_and_dials_nothing(self):
		team, row = self.discrepancy()
		with unittest.mock.patch.object(Target, "post") as post:
			row.correct_gateway()
		post.assert_not_called()
		row.reload()
		self.assertEqual((row.gateway_correction_pending, row.resolution or ""), (1, ""))
		self.assertEqual(self.state(team)[0], D("0.5"), "Grove's own spend is untouched")

	def test_a_pending_row_takes_neither_button(self):
		_team, row = self.discrepancy()
		row.correct_gateway()
		for press in (row.correct_grove, row.correct_gateway):
			with self.assertRaises(frappe.ValidationError):
				press()

	def test_a_row_with_no_store_cannot_be_sent(self):
		_team, row = self.discrepancy()
		row.gateway_store = None
		with self.assertRaises(frappe.ValidationError):
			row.correct_gateway()
		self.assertFalse(frappe.db.get_value("Credit Discrepancy", row.name, "gateway_correction_pending"))

	def test_the_tick_is_handed_a_pending_row_under_its_store_and_name(self):
		_team, row = self.discrepancy()
		self.assertNotIn(row.name, self.owed_ids())
		row.correct_gateway()
		key_hash = frappe.db.get_value("Grove API Key", row.api_key, "key_hash")
		self.assertIn(
			{"key": key_hash, "delta": -100_000_000, "id": row.name}, pending_adjustments()[self.store]
		)

	def test_the_boxs_answer_marks_the_row_corrected_and_it_is_owed_no_more(self):
		team, row = self.discrepancy()
		row.correct_gateway()
		mark_corrected({row.name: 500_000_000})
		row.reload()
		self.assertEqual(
			(
				row.resolution,
				row.gateway_correction_pending,
				D(str(row.correction)),
				D(str(row.gateway_spent)),
			),
			("Gateway corrected", 0, D("-0.1"), D("0.5")),
		)
		self.assertNotIn(row.name, self.owed_ids())
		self.assertEqual(self.state(team)[0], D("0.5"), "Grove's own spend is untouched")
		self.assertEqual(self.money(team), (0.5, 0.5, 0.5), "and so is every money read")

	def owed_ids(self):
		return [body["id"] for body in pending_adjustments().get(self.store, [])]

	def test_correcting_needs_a_system_manager(self):
		_team, row = self.discrepancy()
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			row.correct_grove()


class TestThePushCarriesTheCap(CreditsCase):
	def keys(self):
		section = snapshot.keys_section()
		return {k["prefix"]: k for bucket in section["buckets"].values() for k in bucket["records"]}

	def test_every_store_is_pushed_the_keys_cap_whatever_was_spent(self):
		team, key = self.team(cap=1.25)
		self.pull({key: self.hash(30_000)})
		self.pull({key: self.hash(20_000)}, store=a_store("credits-store-2"))
		pushed = self.keys()[key]
		self.assertEqual(
			(pushed["team"], pushed["prepaid"], pushed["budget"], pushed["limited"]),
			(team, True, 1_250_000_000, False),
		)

	def test_a_suspended_teams_keys_are_pushed_limited_and_a_free_ones_no_budget(self):
		team, key = self.team(cap=0.5)
		other = self.key(team, 0.5)
		frappe.db.set_value("Central Team", team, "suspended", 1)
		_free, free_key = self.team(free=1, cap=5)
		keys = self.keys()
		self.assertEqual(
			(keys[key]["limited"], keys[other]["limited"]),
			(True, True),
			"the team is suspended: every key of it",
		)
		self.assertEqual((keys[free_key]["prepaid"], keys[free_key]["budget"]), (False, 0))

	def test_only_a_published_priced_model_is_routed_and_never_at_a_draft(self):
		unpublished, _pricing = self.priced_model("credits-unpublished-7b", 5)
		new_pricing(self.model, completion_tokens=12)
		table = {
			key: [{"deployment": "d1"}] for key in (self.model_key, "frappe/unpublished", "unpriced/model")
		}
		models = [
			frappe._dict(name=self.model, model_key=self.model_key, published=1),
			frappe._dict(name=unpublished, model_key="frappe/unpublished", published=0),
			frappe._dict(name="no-doc", model_key="unpriced/model", published=1),
		]
		counters = pricing.CounterTable.load().published
		self.assertEqual(
			routes.published_routes(table, models),
			{
				self.model_key: [
					{
						"deployment": "d1",
						"pricing": {
							"id": self.pricing,
							"rates": {"completion_tokens": 10 * NANO},
							"counters": counters,
						},
					}
				]
			},
		)
		self.assertIn(
			{
				"name": "prompt_tokens_above_272k",
				"divisor": 10**6,
				"base": "prompt_tokens",
				"min_prompt_tokens": 272000,
			},
			counters,
		)
