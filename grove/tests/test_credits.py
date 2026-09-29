# Copyright (c) 2026, Frappe and contributors
# See license.txt
"""Prepaid credits end to end on a site: a drain is billed at Grove's price of the pricing the
gateway charged, the gateway's own charge is audited beside it, a discrepancy is corrected on the
side that is wrong, and the push carries every store the amount the user loaded."""

import unittest.mock
from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase

from grove import api, pricing
from grove.grove.doctype.geography.test_geography import make_test_geography
from grove.grove.doctype.grove_user.grove_user import register_user
from grove.grove.doctype.model_pricing.test_model_pricing import enabled_pricing, scheduled_pricing
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
		make_test_geography()
		cls.model, cls.pricing = cls.priced_model("credits-7b", cls.RATE)
		cls.store = a_store("credits-store")
		gateway_module = "grove.grove.doctype.gateway_server.gateway_server"
		with (
			unittest.mock.patch(f"{gateway_module}.sync_fleet_ingress"),
			unittest.mock.patch(f"{gateway_module}.GatewayServer.set_admin_url"),
		):
			machine = frappe.get_doc({"doctype": "Machine", "name": "credits-box", "machine_type": "Gateway"}).insert(ignore_permissions=True)
			cls.gateway = frappe.get_doc({
				"doctype": "Gateway Server", "name": machine.name, "machine": machine.name,
				"gateway_store": cls.store, "is_store_writer": 1,
			}).insert(ignore_permissions=True, ignore_mandatory=True).name
		frappe.db.set_value("Gateway Server", cls.gateway, "status", "Active")

	@classmethod
	def priced_model(cls, model_id, rate):
		model = frappe.get_doc(
			{"doctype": "Model", "model_id": model_id, "modality": "text", "hf_repo": f"org/{model_id}"}
		).insert(ignore_permissions=True).name
		return model, enabled_pricing(model, completion_tokens=rate).name

	def user(self, credit=1, free=0):
		CreditsCase.counter += 1
		doc = frappe.get_doc({
			"doctype": "Grove User", "user": register_user(f"credits-{CreditsCase.counter}@grove.test"), "free": free,
		}).insert(ignore_permissions=True)
		if credit:
			self.credit(doc.name, credit)
		key = frappe.get_doc({"doctype": "Grove API Key", "user": doc.name}).insert(ignore_permissions=True).name
		return doc.name, key

	def credit(self, user, amount, note=None):
		return frappe.get_doc(
			{"doctype": "Grove Credit", "grove_user": user, "amount": amount, "note": note}
		).insert(ignore_permissions=True)

	def balance(self, user):
		return D(str(frappe.db.get_value("Grove User", user, "balance")))

	def hash(self, tokens, cost=None, spent=None, pricing_id=None, requests=1):
		"""What one key's drained hash carries: the counters, tagged with the pricing the gateway
		charged, and the money it wrote. Values are strings, as Redis returns them."""
		p = f"p:{pricing_id or self.pricing}:"
		cost = tokens * self.RATE * 1000 if cost is None else cost
		h = {"completion_tokens": tokens, "request_count": requests, f"m:completion_tokens:{self.model}": tokens,
		     f"{p}completion_tokens": tokens, f"{p}request_count": requests, f"{p}cost": cost, "cost": cost}
		if spent is not None:
			h["user_spent"], h["user_balance"] = spent, NANO - spent
		return {k: str(v) for k, v in h.items()}

	def pull(self, usages, store=None, drain_id=None):
		CreditsCase.counter += 1
		drain_id = drain_id or f"drain-{CreditsCase.counter}"
		with unittest.mock.patch.object(frappe.db, "commit"):
			return usage.record_drains(self.gateway, {drain_id: usages}, gateway_store=store or self.store)

	def state(self, user):
		row = frappe.db.get_value("Grove User", user, ["spent", "credit_exhausted"], as_dict=True)
		return D(str(row.spent)), row.credit_exhausted

	def discrepancies(self, user):
		return frappe.get_all(
			"Credit Discrepancy", filters={"grove_user": user, "resolved": 0},
			fields=["name", "usage_record", "pricing", "gateway_value", "grove_value", "delta"],
		)


class TestADrainIsBilledAtGrovesPrice(CreditsCase):
	def test_a_drain_bills_grove_price_with_both_costs_on_the_record(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(50_000, spent=500_000_000)})
		self.assertEqual(self.state(user), (D("0.5"), 0))
		record = frappe.get_doc("Usage Record", {"api_key": key})
		self.assertEqual((record.cost, record.gateway_cost, record.gateway_store), (0.5, 0.5, self.store))
		[entry] = frappe.parse_json(record.usage)
		self.assertEqual(
			(entry["pricing"], entry["model"], entry["requests"], entry["completion_tokens"], entry["grove_cost"]),
			(self.pricing, self.model, 1, 50_000, 0.5),
		)
		self.assertNotIn("gateway_cost", entry)
		self.assertEqual(frappe.db.get_value("Gateway Spend", {"grove_user": user, "gateway_store": self.store}, "spent"), 0.5)
		self.assertEqual(self.discrepancies(user), [])

	def test_a_resent_drain_bills_nothing_twice(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(50_000)}, drain_id="credits-resent")
		self.pull({key: self.hash(50_000)}, drain_id="credits-resent")
		self.assertEqual(self.state(user), (D("0.5"), 0))

	def test_each_request_is_billed_at_the_pricing_the_gateway_charged(self):
		_model, cheaper = self.priced_model("credits-switch-7b", 5)
		user, key = self.user(credit=5)
		h = self.hash(100_000)
		h.update({k: v for k, v in self.hash(100_000, cost=500_000_000, pricing_id=cheaper).items() if k.startswith("p:")})
		self.pull({key: h})
		self.assertEqual(self.state(user), (D("1.5"), 0))
		self.assertEqual(self.discrepancies(user), [])

	def test_spending_the_balance_exhausts_credit_and_past_it_goes_negative(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(100_000)})
		self.assertEqual(self.state(user), (D("1"), 1))
		self.pull({key: self.hash(50_000)})
		self.assertEqual((self.balance(user), self.state(user)[1]), (D("-0.5"), 1))

	def test_a_top_up_unblocks_and_one_smaller_than_the_debt_does_not(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(150_000)})
		self.credit(user, 0.25)
		self.assertEqual(self.state(user), (D("1.5"), 1))
		self.credit(user, 1)
		self.assertEqual((self.state(user), self.balance(user)), ((D("1.5"), 0), D("0.75")))

	def test_nothing_allocated_blocks_on_save(self):
		blocked, _key = self.user(credit=0)
		self.assertEqual(frappe.db.get_value("Grove User", blocked, "credit_exhausted"), 1)


class TestAFreeUserIsNeverCharged(CreditsCase):
	def record(self, key):
		return frappe.db.get_value("Usage Record", {"api_key": key}, ["billed", "cost", "gateway_cost"], as_dict=True)

	def test_their_usage_is_recorded_with_its_cost_but_spent_and_balance_do_not_move(self):
		free, key = self.user(credit=0, free=1)
		self.pull({key: self.hash(900_000)})
		self.assertEqual((self.state(free), self.balance(free)), ((D("0"), 0), D("0")))
		self.assertEqual(self.record(key), {"billed": 0, "cost": 9, "gateway_cost": 9})

	def test_a_paying_users_record_is_billed(self):
		_user, key = self.user(credit=1)
		self.pull({key: self.hash(50_000)})
		self.assertEqual(self.record(key).billed, 1)

	def test_turning_them_prepaid_starts_at_what_they_load(self):
		user, key = self.user(credit=0, free=1)
		self.pull({key: self.hash(900_000)})
		doc = frappe.get_doc("Grove User", user)
		doc.free = 0
		doc.save()
		self.assertEqual(self.state(user), (D("0"), 1), "nothing loaded yet, and nothing owed")
		self.credit(user, 1)
		self.pull({key: self.hash(30_000)})
		self.assertEqual((self.state(user), self.balance(user)), ((D("0.3"), 0), D("0.7")))

	def test_a_different_gateway_charge_is_no_discrepancy_when_nothing_was_billed(self):
		free, key = self.user(credit=0, free=1)
		self.pull({key: self.hash(50_000, cost=600_000_000)})
		self.assertEqual(self.discrepancies(free), [])

	def test_the_usage_api_counts_their_requests_and_no_cost(self):
		free, key = self.user(credit=0, free=1)
		self.pull({key: self.hash(80_000)})
		email = frappe.db.get_value("Grove User", free, "user")
		result = api.usage([email], period="Today")
		self.assertEqual(result[email], {"requests": 1, "cost": 0})

	def test_untagged_usage_of_a_known_model_is_recorded_and_bills_nothing(self):
		user, key = self.user(credit=1)
		self.pull({key: {"completion_tokens": "100000", "request_count": "1", f"m:completion_tokens:{self.model}": "100000"}})
		self.assertEqual(self.state(user), (D("0"), 0))
		[entry] = frappe.parse_json(frappe.db.get_value("Usage Record", {"api_key": key}, "usage"))
		self.assertEqual((entry["pricing"], entry["completion_tokens"], entry["grove_cost"]), (None, 100_000, 0))

	def test_an_unknown_pricing_leaves_the_users_share_stuck_and_unacknowledged(self):
		user, key = self.user(credit=1)
		self.assertEqual(self.pull({key: self.hash(10_000, pricing_id="no-such-pricing")}, drain_id="credits-unknown"), (1, {}, [], 1))
		self.assertEqual(self.state(user), (D("0"), 0))
		self.assertTrue(frappe.db.exists("Stuck Usage", {"grove_user": user, "resolved": 0}))

	def test_a_stale_form_keeps_the_databases_spent(self):
		user, key = self.user(credit=1)
		doc = frappe.get_doc("Grove User", user)
		self.pull({key: self.hash(70_000)})
		doc.log_payloads = 1
		doc.save()
		self.assertEqual(self.state(user), (D("0.7"), 0))

	def test_the_ledger_is_append_only_and_refuses_zero_and_unexplained_negatives(self):
		user, _key = self.user(credit=1)
		with self.assertRaises(frappe.ValidationError):
			self.credit(user, 0)
		with self.assertRaises(frappe.ValidationError):
			self.credit(user, -0.5)
		entry = self.credit(user, -0.5, note="refund")
		self.assertEqual(self.balance(user), D("0.5"))
		entry.amount = 5
		with self.assertRaises(frappe.ValidationError):
			entry.save()
		entry.reload()
		with self.assertRaises(frappe.ValidationError):
			entry.delete()


class TestTheUsageApiSumsInTheDatabase(CreditsCase):
	def test_requests_and_cost_per_user_and_model_over_a_period(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000)})
		self.pull({key: self.hash(30_000, requests=2)})
		email = frappe.db.get_value("Grove User", user, "user")
		result = api.usage([email], period="Today")
		self.assertEqual(result[email], {"requests": 3, "cost": 0.8})
		self.assertEqual(result["model_summary"], [{"model": self.model, "requests": 3, "cost": 0.8}])
		self.assertTrue(result["as_of"].endswith("Z"))
		self.assertEqual(api.usage([email], period="Yesterday")[email], {"requests": 0, "cost": 0})


class TestTheGatewaysChargeIsAudited(CreditsCase):
	def test_a_different_charge_is_one_discrepancy_on_its_record_and_grove_bills_its_own_price(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000, cost=600_000_000)})
		[row] = self.discrepancies(user)
		record = frappe.db.get_value("Usage Record", {"api_key": key})
		self.assertEqual(
			(row.usage_record, row.pricing, D(str(row.gateway_value)), D(str(row.grove_value)), D(str(row.delta))),
			(record, self.pricing, D("0.6"), D("0.5"), D("0.1")),
		)
		self.assertEqual(self.state(user), (D("0.5"), 0))

	def test_truncation_up_to_seven_nano_a_request_is_not_a_discrepancy(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000, cost=500_000_000 - 14, requests=2)})
		self.assertEqual(self.discrepancies(user), [])
		self.pull({key: self.hash(50_000, cost=500_000_000 - 15, requests=2)})
		self.assertEqual(len(self.discrepancies(user)), 1)


class TestADiscrepancyIsCorrectedOnTheWrongSide(CreditsCase):
	def discrepancy(self):
		user, key = self.user(credit=5)
		self.pull({key: self.hash(50_000, cost=600_000_000)})
		[row] = self.discrepancies(user)
		return user, frappe.get_doc("Credit Discrepancy", row.name)

	def test_grove_is_wrong_moves_spent_by_the_delta_and_settles(self):
		user, row = self.discrepancy()
		row.correct_grove()
		row.reload()
		self.assertEqual((self.state(user)[0], self.balance(user)), (D("0.6"), D("4.4")))
		self.assertEqual((row.resolved, row.resolution, D(str(row.correction))), (1, "Grove corrected", D("0.1")))
		with self.assertRaises(frappe.ValidationError):
			row.correct_grove()

	def test_gateway_is_wrong_sends_the_adjustment_once_under_the_rows_name(self):
		user, row = self.discrepancy()
		box = unittest.mock.Mock(error=None)
		box.post.return_value = {"spent": 500_000_000, "applied": True}
		with unittest.mock.patch.object(Target, "resolve", return_value=box) as resolve:
			row.correct_gateway()
		box.post.assert_called_once_with("spend-adjust", {"user": user, "delta": -100_000_000, "id": row.name})
		self.assertEqual(resolve.call_args.args, ("Gateway Server", self.gateway))
		row.reload()
		self.assertEqual((row.resolved, row.resolution, D(str(row.gateway_spent))), (1, "Gateway corrected", D("0.5")))
		self.assertEqual(self.state(user)[0], D("0.5"), "Grove's own spend is untouched")

	def test_a_failed_gateway_correction_leaves_the_row_open(self):
		_user, row = self.discrepancy()
		box = unittest.mock.Mock(error=None)
		box.post.side_effect = ConnectionError("box gone")
		with unittest.mock.patch.object(Target, "resolve", return_value=box), self.assertRaises(ConnectionError):
			row.correct_gateway()
		row.reload()
		self.assertEqual(row.resolved, 0)

	def test_correcting_needs_a_system_manager(self):
		_user, row = self.discrepancy()
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			row.correct_grove()


class TestThePushCarriesTheAmountLoaded(CreditsCase):
	def users(self):
		with unittest.mock.patch.object(snapshot, "effective_groups", return_value=[]), unittest.mock.patch.object(snapshot, "effective_keys", return_value=[]):
			section = snapshot.gateway_snapshot("", {})["users"]
		return {u["name"]: u for bucket in section["buckets"].values() for u in bucket["records"]}

	def test_every_store_is_pushed_the_sum_of_credits_whatever_was_spent(self):
		user, key = self.user(credit=1)
		self.credit(user, 0.25)
		self.pull({key: self.hash(30_000)})
		self.pull({key: self.hash(20_000)}, store=a_store("credits-store-2"))
		pushed = self.users()[user]
		self.assertEqual((pushed["prepaid"], pushed["budget"], pushed["limited"]), (True, 1_250_000_000, False))

	def test_an_exhausted_user_is_pushed_limited_and_a_free_one_no_budget(self):
		user, key = self.user(credit=1)
		self.pull({key: self.hash(100_000)})
		free, _key = self.user(credit=0, free=1)
		users = self.users()
		self.assertIs(users[user]["limited"], True)
		self.assertEqual((users[free]["prepaid"], users[free]["budget"]), (False, 0))

	def test_routes_carry_the_pricing_and_the_scheduled_one_with_its_utc_activation(self):
		table = {self.model: [{"deployment": "d1"}], "unpriced/model": [{"deployment": "d3"}]}
		upcoming = scheduled_pricing(self.model, completion_tokens=12)
		self.addCleanup(frappe.db.set_value, "Model Pricing", upcoming.name, "status", "Disabled")
		routes.add_pricing(table)
		[row] = table[self.model]
		self.assertEqual(row["pricing"], {"id": self.pricing, "rates": {"completion_tokens": 10 * NANO}})
		self.assertEqual((row["next_pricing"]["id"], row["next_pricing"]["rates"]), (upcoming.name, {"completion_tokens": 12 * NANO}))
		self.assertEqual(row["next_pricing"]["activates_at"], routes.utc_timestamp(upcoming.activates_at))
		self.assertTrue(row["next_pricing"]["activates_at"].endswith("Z"))
		self.assertEqual(table["unpriced/model"], [{"deployment": "d3"}])
