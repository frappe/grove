# Copyright (c) 2026, Frappe and contributors
# See license.txt

import hashlib
import json
import unittest.mock
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from grove import utils
from grove.billing import usage_evidence
from grove.billing.doctype.usage_record.usage_record import landing_window
from grove.billing.pricing import CounterTable, PriceBook
from grove.billing.usage_evidence import DayEvidence, fit, size
from grove.billing.usage_report import CentralReply, ReportLine, UsageReport
from grove.pathway import routes
from grove.tests.test_usage_report import NOON, ReportCase
from grove.utils import site_datetime

DAY = NOON.date()


def record(team, at, *entries, discrepancy=None, day=None):
	"""A record that landed at `at` (UTC) holding the given usage entries, served on `day` (the
	landing day by default). → its name."""
	doc = frappe.get_doc(
		{
			"doctype": "Usage Record",
			"api_key": "k",
			"team": team,
			"day": day or at.date(),
			"drain_id": frappe.generate_hash(length=12),
			"cost_discrepancy": discrepancy,
			"request_count": 1,
			"cost": 0,
			"gateway_cost": 0,
			"usage": json.dumps(list(entries)),
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Usage Record", doc.name, "creation", site_datetime(at), update_modified=False)
	return doc.name


def entry(pricing, cost, requests=1, model="m1", **counters):
	return {"model": model, "pricing": pricing, "requests": requests, "grove_cost": cost, **counters}


def book(**rates_by_pricing):
	"""A price book holding the site's counters and the given {pricing: {counter: rate}}."""
	prices = PriceBook()
	prices.counters = CounterTable.load()
	prices.pricings = {
		pricing: frappe._dict(
			model_key="m1", rates={counter: Decimal(rate) for counter, rate in rates.items()}
		)
		for pricing, rates in rates_by_pricing.items()
	}
	return prices


def digest(names):
	return hashlib.sha256(",".join(sorted(names)).encode()).hexdigest()


class EvidenceCase(IntegrationTestCase):
	def setUp(self):
		self.team = f"t-{frappe.generate_hash(length=8)}"

	def evidence(self, model="m1", prices=None):
		return DayEvidence(landing_window(DAY), prices or book()).get_lines()[(self.team, model)]


class TestTheEvidenceMatchesTheRecords(EvidenceCase):
	def test_requests_tokens_pricings_and_records_are_those_summed(self):
		first = record(
			self.team,
			NOON - timedelta(hours=2),
			entry("p1", 1.25, requests=2, prompt_tokens=1000, cached_tokens=200, completion_tokens=50),
			entry("p1", 0.5, requests=1, prompt_tokens=10, prompt_tokens_above_272k=300000),
		)
		second = record(self.team, NOON - timedelta(hours=1), entry("p2", 0.000000001, completion_tokens=7))
		record(self.team, NOON - timedelta(days=1), entry("p1", 9, prompt_tokens=99))

		evidence = self.evidence(prices=book(p1={"prompt_tokens": "0.3", "completion_tokens": "1.2"}))

		self.assertEqual(
			evidence,
			{
				"v": 1,
				"requests": 4,
				"tokens_in": 301010,
				"tokens_out": 57,
				"pricings": [
					{
						"id": "p1",
						"requests": 3,
						"counters": {
							"prompt_tokens": 1010,
							"cached_tokens": 200,
							"completion_tokens": 50,
							"prompt_tokens_above_272k": 300000,
						},
						"rates": {"completion_tokens": "1.2", "prompt_tokens": "0.3"},
						"cost": "1.750000000",
					},
					{
						"id": "p2",
						"requests": 1,
						"counters": {"completion_tokens": 7},
						"rates": {},
						"cost": "0.000000001",
					},
				],
				"records": {
					"count": 2,
					"sha256": digest([first, second]),
					"first_at": "2026-11-20T10:00:00Z",
					"last_at": "2026-11-20T11:00:00Z",
				},
				"corrections": {"count": 0, "cost": "0.000000000", "discrepancies": []},
				"truncated": False,
			},
		)
		self.assertLessEqual(size(evidence), usage_evidence.MAXIMUM_BYTES)

	def test_the_digest_is_of_the_sorted_names_whatever_order_the_records_landed_in(self):
		names = [record(self.team, NOON, entry("p1", 1)) for _ in range(3)]
		for offset, name in enumerate(sorted(names, reverse=True)):
			frappe.db.set_value(
				"Usage Record",
				name,
				"creation",
				site_datetime(NOON - timedelta(hours=3 - offset)),
				update_modified=False,
			)

		records = self.evidence()["records"]

		self.assertEqual(records["sha256"], digest(names))
		self.assertEqual(records["first_at"], "2026-11-20T09:00:00Z")

	def test_a_digest_cut_by_the_session_limit_is_left_out_and_marked_truncated(self):
		for _ in range(2):
			record(self.team, NOON, entry("p1", 1))

		limit = frappe.db.sql("select @@session.group_concat_max_len")[0][0]
		self.addCleanup(frappe.db.sql, f"set session group_concat_max_len = {int(limit)}")
		frappe.db.sql("set session group_concat_max_len = 4")
		evidence = self.evidence()

		self.assertNotIn("sha256", evidence["records"])
		self.assertEqual(evidence["records"]["count"], 2)
		self.assertTrue(evidence["truncated"])


class TestACorrectionIsCountedApart(EvidenceCase):
	def test_a_correction_record_is_a_correction_not_a_pricing(self):
		usage = record(self.team, NOON - timedelta(hours=1), entry("p1", 1.5, prompt_tokens=10))
		correction = record(
			self.team, NOON - timedelta(minutes=30), entry("p1", -0.5, requests=0), discrepancy="cd-1"
		)

		evidence = self.evidence()

		self.assertEqual(
			evidence["corrections"], {"count": 1, "cost": "-0.500000000", "discrepancies": ["cd-1"]}
		)
		self.assertEqual([(p["id"], p["cost"]) for p in evidence["pricings"]], [("p1", "1.500000000")])
		self.assertEqual(evidence["records"]["count"], 2)
		self.assertEqual(evidence["records"]["sha256"], digest([usage, correction]))

	def test_at_most_twenty_discrepancies_are_named_but_every_correction_is_counted(self):
		names = [f"cd-{index:02}" for index in range(usage_evidence.MAXIMUM_DISCREPANCIES + 1)]
		for name in names:
			record(self.team, NOON, entry("p1", -0.5, requests=0), discrepancy=name)

		corrections = self.evidence()["corrections"]

		self.assertEqual(corrections["count"], len(names))
		self.assertEqual(corrections["discrepancies"], names[: usage_evidence.MAXIMUM_DISCREPANCIES])


class TestEightKilobytesAtMost(EvidenceCase):
	def test_more_than_eight_pricings_keep_the_costliest_eight_without_counters_or_rates(self):
		record(self.team, NOON, *(entry(f"p{index}", index, prompt_tokens=index) for index in range(1, 11)))

		evidence = self.evidence()

		self.assertTrue(evidence["truncated"])
		self.assertEqual([p["id"] for p in evidence["pricings"]], [f"p{index}" for index in range(10, 2, -1)])
		self.assertEqual(set(evidence["pricings"][0]), {"id", "requests", "cost"})

	def test_evidence_over_eight_kilobytes_drops_its_counters_and_rates(self):
		rates = {f"counter_{index}": "0." + "1" * 40 for index in range(200)}
		evidence = {
			"v": 1,
			"pricings": [
				{"id": "p1", "requests": 1, "counters": {"prompt_tokens": 1}, "rates": rates, "cost": "1"}
			],
			"truncated": False,
		}

		fitted = fit(evidence)

		self.assertGreater(size(evidence), usage_evidence.MAXIMUM_BYTES)
		self.assertLessEqual(size(fitted), usage_evidence.MAXIMUM_BYTES)
		self.assertEqual(fitted["pricings"], [{"id": "p1", "requests": 1, "cost": "1"}])
		self.assertTrue(fitted["truncated"])


class TestTheLineCarriesItsEvidence(ReportCase):
	def test_the_key_and_comparison_ignore_the_evidence(self):
		bare = ReportLine("t", "m1", DAY, Decimal("1.5"))
		with_evidence = ReportLine("t", "m1", DAY, Decimal("1.5"), {"v": 1})

		self.assertEqual(bare, with_evidence)
		self.assertEqual(hash(bare), hash(with_evidence))
		self.assertEqual(bare.key, with_evidence.key)
		self.assertEqual(with_evidence.payload["evidence"], {"v": 1})

	def test_each_line_sent_carries_its_evidence(self):
		name = record(self.team, NOON - timedelta(hours=1), entry("p1", 1, prompt_tokens=5))

		self.report()

		line = next(
			line for body in self.central.bodies for line in body["lines"] if line["subject"] == self.team
		)
		self.assertEqual(line["evidence"]["records"]["sha256"], digest([name]))
		self.assertEqual(line["evidence"]["tokens_in"], 5)

	def test_the_evidence_adds_up_to_the_line_over_the_same_landing_window(self):
		for module in (utils, routes):
			patcher = unittest.mock.patch.object(module, "get_system_timezone", return_value="Asia/Kolkata")
			patcher.start()
			self.addCleanup(patcher.stop)
		midnight = NOON.replace(hour=0)
		record(self.team, midnight - timedelta(seconds=1), entry("p1", 7))
		record(self.team, midnight, entry("p1", 1.25), day=DAY - timedelta(days=1))
		record(self.team, NOON - timedelta(hours=1), entry("p2", 0.5, prompt_tokens=10))
		record(self.team, NOON, entry("p1", -0.25, requests=0), discrepancy="cd-1")

		line = next(
			line
			for line in UsageReport(frappe.get_single("Grove Settings"), NOON).get_day_lines(DAY)
			if line.team == self.team
		)

		pricings = sum(Decimal(pricing["cost"]) for pricing in line.evidence["pricings"])
		self.assertEqual(line.cost, Decimal("1.5"))
		self.assertEqual(pricings + Decimal(line.evidence["corrections"]["cost"]), line.cost)
		self.assertEqual(line.evidence["records"]["count"], 3)
		self.assertEqual(line.evidence["records"]["first_at"], "2026-11-20T00:00:00Z")

	def test_a_reply_with_or_without_underived_parses(self):
		for message in ({"accepted": 1, "rejected": []}, {"accepted": 1, "rejected": [], "underived": 1}):
			reply = {"message": message}
			response = SimpleNamespace(json=lambda reply=reply: reply, text=json.dumps(reply))

			verdict = CentralReply.parse(response).get_verdict(ReportLine("t", "m1", DAY, Decimal(1)))

			self.assertEqual(verdict["status"], "Confirmed")
