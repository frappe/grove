# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import frappe
import requests

from grove.billing.doctype.usage_record.usage_record import money_rows
from grove.billing.doctype.usage_report_day import usage_report_day
from grove.utils import site_datetime

REPORT_ROUTE = "/api/method/central.billing.api.metering.report"
SERVICE = "ai"
METER = "cost"
REPORT_FIELDS = ("central_url", "central_report_secret")
NANO_PER_USD = 10**9
# Inside the Usage Record purge horizon (90 days), so no day sent can be partly purged.
WINDOW_DAYS = 30
BATCH_LINES = 1000
TIMEOUT = 30


@dataclass(frozen=True)
class ReportLine:
	"""One team's cost for one model on one UTC landing day, so far."""

	team: str
	model: str
	day: date
	cost: Decimal

	@property
	def key(self):
		return (self.team, self.model, str(self.day))

	@property
	def value(self):
		return int(self.cost * NANO_PER_USD)

	@property
	def payload(self):
		return {
			"subject": self.team,
			"meter": METER,
			"day": str(self.day),
			"value": self.value,
			"dimension": self.model,
			"amount": float(self.cost),
			"currency": "USD",
		}


class UsageReport:
	"""The hourly push of AI cost to Central: per team, model and UTC landing day, the day's cost
	so far, read off the Usage Records, signed with the `ai` Service secret."""

	def __init__(self, settings, as_of):
		self.settings = settings
		self.as_of = as_of.replace(microsecond=0)

	@classmethod
	def load(cls):
		"""Taken before any record is read, so a later report always carries at least as much."""
		return cls(frappe.get_single("Grove Settings"), datetime.now(UTC))

	@property
	def secret(self):
		return self.settings.get_password("central_report_secret", raise_exception=False) or ""

	@property
	def problems(self):
		"""Each blank setting the report needs, named."""
		return self.settings.get_unset_field_problems(REPORT_FIELDS)

	@property
	def is_ready(self):
		return not self.settings.pause_usage_report and not self.problems

	@property
	def today(self):
		return self.as_of.date()

	@property
	def first_day(self):
		return self.today - timedelta(days=WINDOW_DAYS - 1)

	@property
	def first_resent_day(self):
		"""Grove commits a whole gateway answer at once, so a record stamped just before midnight
		can appear after it: the last few landing days are sent every run."""
		return self.today - timedelta(days=self.settings.report_resend_days - 1)

	def send(self):
		if not self.is_ready:
			return
		usage_report_day.prune(self.first_day)
		sent = usage_report_day.get_sent_days(self.first_day)
		due = [line for line in self.get_lines() if self.is_due(line, sent.get(line.key))]
		for start in range(0, len(due), BATCH_LINES):
			batch = due[start : start + BATCH_LINES]
			usage_report_day.record_outcomes(self.get_outcomes(batch, self.post(batch)), sent)
			frappe.db.commit()

	def get_lines(self):
		days = (self.first_day + timedelta(days=offset) for offset in range(WINDOW_DAYS))
		return [line for day in days for line in self.get_day_lines(day)]

	def get_day_lines(self, day):
		"""Summed in SQL as decimals and read as text: the database driver turns a decimal into a
		float. The landing day's bounds are UTC; `creation` is site time."""
		table, params = money_rows({}, since=site_datetime(day), until=site_datetime(day + timedelta(days=1)))
		rows = frappe.db.sql(
			f"""select team, coalesce(model, '') as model,
			cast(sum(cost) as char) as cost from {table}
			where team is not null and team != '' group by team, coalesce(model, '')""",
			params,
			as_dict=True,
		)
		return [ReportLine(row.team, row.model, day, Decimal(row.cost or 0)) for row in rows]

	def is_due(self, line, row):
		"""A refused line waits for its value to change; any other is sent until confirmed, and
		every recent day is sent again."""
		if not row or row.value_sent != line.value:
			return True
		if row.refused:
			return False
		return not row.confirmed_at or line.day >= self.first_resent_day

	def post(self, lines):
		"""Central's `rejected`, keyed like the lines. Thrown on anything but 200: the lines stay
		unconfirmed for the next run."""
		body = json.dumps(
			{"as_of": self.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": [line.payload for line in lines]}
		)
		response = requests.post(
			f"{self.settings.central_url.rstrip('/')}{REPORT_ROUTE}",
			data=body.encode(),
			headers={
				"Content-Type": "application/json",
				"X-Central-Service": SERVICE,
				"X-Central-Signature": sign(self.secret, body.encode()),
			},
			timeout=TIMEOUT,
		)
		if response.status_code != 200:
			frappe.throw(f"Central refused the usage report: {response.status_code} {response.text[:200]}")
		rejected = response.json()["message"]["rejected"]
		return {(line["subject"], line.get("dimension") or "", line["day"]): line for line in rejected}

	def get_outcomes(self, lines, rejected):
		"""{line key: the outbox fields} for each line sent."""
		now = frappe.utils.now_datetime()
		outcomes = {}
		for line in lines:
			refusal = rejected.get(line.key)
			outcomes[line.key] = {
				"value_sent": line.value,
				"as_of": site_datetime(self.as_of),
				"sent_at": now,
				"confirmed_at": None if refusal else now,
				"refused": int(bool(refusal) and not refusal.get("retry")),
				"reason": (refusal or {}).get("reason") or "",
			}
		return outcomes


def sign(secret, body):
	"""base64 HMAC-SHA256 of the raw body, as Central checks it."""
	return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def report_usage():
	"""Tick, hourly. Paused or unconfigured, it sends nothing."""
	UsageReport.load().send()
