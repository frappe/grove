# Copyright (c) 2026, Frappe and contributors
# See license.txt

import base64
import hashlib
import hmac
import json
import unittest.mock
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import frappe
import requests
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from grove.api import control
from grove.billing import usage_report
from grove.billing.usage_report import UsageReport
from grove.patches.v1_0 import set_report_resend_days
from grove.tests.test_api_permissions import other_user
from grove.utils import site_datetime

SECRET = "report-secret"
NOON = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = "2026-11-20"


def landed(team, at, *entries):
	"""A record that landed at `at` (UTC) holding (model, grove_cost) entries."""
	doc = frappe.get_doc(
		{
			"doctype": "Usage Record",
			"api_key": "k",
			"team": team,
			"day": at.date(),
			"drain_id": frappe.generate_hash(length=12),
			"request_count": 1,
			"cost": 0,
			"gateway_cost": 0,
			"usage": json.dumps(
				[{"model": model, "requests": 1, "grove_cost": cost} for model, cost in entries]
			),
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Usage Record", doc.name, "creation", site_datetime(at), update_modified=False)


class FakeCentral:
	"""Central's report endpoint: checks the signature, refuses the lines it is told to."""

	def __init__(self):
		self.bodies, self.refuse, self.failure = [], {}, None

	def post(self, url, data, headers, timeout):
		if self.failure:
			raise self.failure
		expected = base64.b64encode(hmac.new(SECRET.encode(), data, hashlib.sha256).digest()).decode()
		if headers["X-Central-Service"] != "ai" or headers["X-Central-Signature"] != expected:
			return SimpleNamespace(status_code=403, text="signature mismatch")
		body = json.loads(data)
		self.bodies.append(body)
		rejected = [
			{
				**{key: line[key] for key in ("subject", "meter", "day", "dimension")},
				"reason": "no",
				"retry": retry,
			}
			for line in body["lines"]
			if (retry := self.refuse.get((line["subject"], line["dimension"], line["day"]))) is not None
		]
		message = {"accepted": len(body["lines"]) - len(rejected), "rejected": rejected}
		return SimpleNamespace(status_code=200, json=lambda: {"message": message})

	@property
	def lines(self):
		"""{(subject, dimension, day): value} of the last report."""
		return {
			(line["subject"], line["dimension"], line["day"]): line["value"]
			for line in self.bodies[-1]["lines"]
		}


class ReportCase(IntegrationTestCase):
	def setUp(self):
		self.central = FakeCentral()
		# The report commits per batch; patched out so the test's rows roll back.
		for patcher in (
			unittest.mock.patch.object(usage_report.requests, "post", self.central.post),
			unittest.mock.patch.object(frappe.db, "commit"),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		frappe.db.set_single_value(
			"Grove Settings",
			{"central_url": "https://central.test", "pause_usage_report": 0, "report_resend_days": 3},
		)
		set_encrypted_password("Grove Settings", "Grove Settings", SECRET, "central_report_secret")
		frappe.db.delete("Usage Report Day")
		self.team = f"t-{frappe.generate_hash(length=8)}"
		self.read_days = []

	def report(self, at=NOON):
		self.central.bodies = []
		with unittest.mock.patch.object(
			UsageReport, "get_day_lines", autospec=True, side_effect=UsageReport.get_day_lines
		) as get_day_lines:
			UsageReport(frappe.get_single("Grove Settings"), at).send()
		self.read_days = [str(call.args[1]) for call in get_day_lines.call_args_list]
		return self.central.lines if self.central.bodies else {}

	def mine(self, lines):
		return {key: value for key, value in lines.items() if key[0] == self.team}


class TestALineIsTheLandingDaysDecimalSum(ReportCase):
	def test_each_team_model_and_landing_day_is_summed_to_the_nano(self):
		landed(self.team, NOON - timedelta(hours=1), ("m1", 1.123456789), ("m2", 0.000000001))
		landed(self.team, NOON - timedelta(hours=2), ("m1", 2.000000003))
		landed(self.team, NOON - timedelta(days=1), ("m1", 0.1))

		lines = self.mine(self.report())

		self.assertEqual(
			lines,
			{
				(self.team, "m1", TODAY): 3_123_456_792,
				(self.team, "m2", TODAY): 1,
				(self.team, "m1", "2026-11-19"): 100_000_000,
			},
		)
		line = next(
			line
			for line in self.central.bodies[-1]["lines"]
			if (line["subject"], line["dimension"], line["day"]) == (self.team, "m1", TODAY)
		)
		self.assertEqual((line["meter"], line["currency"], line["amount"]), ("cost", "USD", 3.123456792))
		self.assertTrue(self.central.bodies[-1]["as_of"].endswith("Z"))

	def test_a_negative_correction_reduces_its_line_and_one_with_no_model_is_sent_blank(self):
		landed(self.team, NOON - timedelta(hours=1), ("m1", 1.5))
		landed(self.team, NOON - timedelta(minutes=30), ("m1", -0.5))
		landed(self.team, NOON - timedelta(minutes=20), ("", -0.25))

		lines = self.mine(self.report())

		self.assertEqual(lines[(self.team, "m1", TODAY)], 1_000_000_000)
		self.assertEqual(lines[(self.team, "", TODAY)], -250_000_000)


class TestALateCommitBillsOnItsLandingDay(ReportCase):
	def test_a_record_stamped_before_midnight_and_committed_after_is_resent_on_its_day(self):
		before_midnight = datetime(2026, 11, 20, 23, 59, 50, tzinfo=UTC)
		landed(self.team, NOON, ("m1", 1))
		self.report(at=datetime(2026, 11, 20, 23, 59, 55, tzinfo=UTC))

		landed(self.team, before_midnight, ("m1", 2))
		lines = self.mine(self.report(at=datetime(2026, 11, 21, 0, 30, tzinfo=UTC)))

		self.assertEqual(lines, {(self.team, "m1", TODAY): 3_000_000_000})

	def test_confirmed_days_are_resent_only_within_the_resend_days(self):
		for days_ago in (1, 2, 3):
			landed(self.team, NOON - timedelta(days=days_ago), ("m1", 1))
		self.report()

		lines = self.mine(self.report())

		self.assertEqual({key[2] for key in lines}, {"2026-11-19", "2026-11-18"})


class TestOnlyDaysThatCanChangeAreRead(ReportCase):
	def test_the_first_run_reads_the_whole_window(self):
		self.report()

		self.assertEqual(len(self.read_days), 30)
		self.assertEqual((self.read_days[0], self.read_days[-1]), ("2026-10-22", TODAY))

	def test_a_confirmed_day_past_the_resend_days_is_not_read_again(self):
		landed(self.team, NOON, ("m1", 1))
		landed(self.team, NOON - timedelta(days=5), ("m1", 1))
		self.report()

		landed(self.team, NOON - timedelta(days=5), ("m1", 1))
		lines = self.mine(self.report())

		self.assertEqual(self.read_days, ["2026-11-18", "2026-11-19", TODAY])
		self.assertEqual(set(lines), {(self.team, "m1", TODAY)})

	def test_a_pending_day_past_the_resend_days_is_read_until_confirmed(self):
		landed(self.team, NOON, ("m1", 1))
		landed(self.team, NOON - timedelta(days=5), ("m1", 1))
		self.central.refuse = {(self.team, "m1", "2026-11-15"): True}
		self.report()
		self.assertEqual(
			frappe.db.get_value("Usage Report Day", {"team": self.team, "day": "2026-11-15"}, "status"),
			"Pending",
		)
		self.central.refuse = {}

		lines = self.mine(self.report())

		self.assertEqual(self.read_days, ["2026-11-15", "2026-11-18", "2026-11-19", TODAY])
		self.assertEqual(lines[(self.team, "m1", "2026-11-15")], 1_000_000_000)
		self.report()
		self.assertNotIn("2026-11-15", self.read_days)

	def test_a_run_after_a_pause_reads_every_day_since_the_last_one_sent(self):
		landed(self.team, NOON - timedelta(days=10), ("m1", 1))
		self.report(at=NOON - timedelta(days=10))

		landed(self.team, NOON - timedelta(days=6), ("m1", 2))
		lines = self.mine(self.report())

		self.assertEqual((self.read_days[0], len(self.read_days)), ("2026-11-10", 11))
		self.assertEqual(lines, {(self.team, "m1", "2026-11-14"): 2_000_000_000})


class TestCentralsAnswer(ReportCase):
	def test_unconfirmed_days_carry_over_a_central_outage(self):
		landed(self.team, NOON - timedelta(days=5), ("m1", 1))
		landed(self.team, NOON - timedelta(days=6), ("m1", 2))
		self.central.failure = requests.ConnectionError("down")
		with self.assertRaises(requests.ConnectionError):
			self.report()

		self.central.failure = None
		lines = self.mine(self.report(at=NOON + timedelta(days=1)))

		self.assertEqual({key[2] for key in lines}, {"2026-11-15", "2026-11-14"})
		self.assertFalse(
			self.mine(self.report(at=NOON + timedelta(days=1))), "confirmed, outside resend days"
		)

	def test_a_refused_day_is_not_resent_until_its_value_changes(self):
		yesterday = "2026-11-19"
		landed(self.team, NOON - timedelta(days=1), ("m1", 1))
		landed(self.team, NOON - timedelta(days=1), ("m2", 1))
		self.central.refuse = {(self.team, "m1", yesterday): False, (self.team, "m2", yesterday): True}
		self.report()
		self.central.refuse = {}

		self.assertEqual(set(self.mine(self.report())), {(self.team, "m2", yesterday)})
		self.assertEqual(
			frappe.db.get_all(
				"Usage Report Day", {"team": self.team}, ["model", "status", "reason"], order_by="model"
			),
			[
				{"model": "m1", "status": "Refused", "reason": "no"},
				{"model": "m2", "status": "Confirmed", "reason": ""},
			],
		)

		landed(self.team, NOON - timedelta(days=1), ("m1", 1))
		self.assertEqual(
			self.mine(self.report()),
			{(self.team, "m1", yesterday): 2_000_000_000, (self.team, "m2", yesterday): 1_000_000_000},
		)

	def test_a_bad_signature_fails_the_run_and_confirms_nothing(self):
		landed(self.team, NOON, ("m1", 1))
		set_encrypted_password("Grove Settings", "Grove Settings", "wrong", "central_report_secret")

		with self.assertRaises(frappe.ValidationError):
			self.report()

		self.assertFalse(frappe.db.exists("Usage Report Day", {"team": self.team}))


class TestTheWindow(ReportCase):
	def test_a_day_older_than_thirty_days_is_never_sent_and_its_outbox_row_goes(self):
		for days_ago in (30, 29):
			landed(self.team, NOON - timedelta(days=days_ago), ("m1", 1))
		self.report(at=NOON - timedelta(days=10))
		self.assertTrue(frappe.db.exists("Usage Report Day", {"team": self.team, "day": "2026-10-21"}))

		for days_ago in (30, 29):
			landed(self.team, NOON - timedelta(days=days_ago), ("m1", 1))
		lines = self.mine(self.report())

		self.assertEqual(set(lines), {(self.team, "m1", "2026-10-22")})
		self.assertFalse(frappe.db.exists("Usage Report Day", {"team": self.team, "day": "2026-10-21"}))

	def test_a_paused_or_unconfigured_report_makes_no_call(self):
		landed(self.team, NOON, ("m1", 1))
		frappe.db.set_single_value("Grove Settings", "pause_usage_report", 1)
		self.assertEqual(self.report(), {})

		frappe.db.set_single_value("Grove Settings", "pause_usage_report", 0)
		set_encrypted_password("Grove Settings", "Grove Settings", "", "central_report_secret")
		self.assertEqual(self.report(), {})
		self.assertEqual(UsageReport.load().problems, ["Set Central Report Secret in Grove Settings."])


class TestCentralSetsTheReportSecret(IntegrationTestCase):
	def test_only_a_control_client_sets_it(self):
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(other_user("report-outsider@example.com", "System Manager"))
		with self.assertRaises(frappe.PermissionError):
			control.set_report_secret("stolen")

		frappe.set_user(other_user("report-central@example.com", control.CONTROL_ROLE))
		control.set_report_secret("from-central")

		settings = frappe.get_single("Grove Settings")
		self.assertEqual(settings.get_password("central_report_secret"), "from-central")


class TestResendDays(IntegrationTestCase):
	def test_a_site_with_no_resend_days_row_gets_the_default(self):
		frappe.db.delete("Singles", {"doctype": "Grove Settings", "field": "report_resend_days"})
		frappe.db.delete("Singles", {"doctype": "Grove Settings", "field": "pause_usage_report"})
		self.assertFalse(frappe.db.get_single_value("Grove Settings", "report_resend_days"))

		set_report_resend_days.execute()

		settings = frappe.get_single("Grove Settings")
		self.assertEqual((settings.report_resend_days, settings.pause_usage_report), (3, 0))

	def test_resend_days_below_one_is_refused(self):
		settings = frappe.get_single("Grove Settings")
		settings.report_resend_days = 0

		with self.assertRaisesRegex(frappe.ValidationError, "Resend Days must be at least 1"):
			settings.save()
