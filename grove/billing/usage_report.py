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
from grove.billing.doctype.usage_report_day.usage_report_day import (
	CONFIRMED,
	PENDING,
	REFUSED,
	UsageReportDays,
	report_key,
)
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
		return report_key(self.team, self.model, self.day)

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
		return self.today - timedelta(days=self.settings.resend_days - 1)

	def send(self):
		"""Day by day, oldest first, committing each batch: a run killed part way keeps the days it
		sent, and the next one goes on from the newest day in the outbox."""
		if not self.is_ready:
			return
		outbox = UsageReportDays(self.first_day)
		for day in self.get_days(outbox):
			self.send_day(outbox, day)

	def send_day(self, outbox, day):
		due = [line for line in self.get_day_lines(day) if self.is_due(line, outbox.get(line.key))]
		for start in range(0, len(due), BATCH_LINES):
			batch = due[start : start + BATCH_LINES]
			outbox.record(self.get_outcomes(batch, self.post(batch)))
			frappe.db.commit()

	def get_days(self, outbox):
		"""The landing days to read, oldest first. A Usage Record is stamped as it is written, so
		only the resend days can still change; the days still Pending and every day since the
		newest one in the outbox (left unsent by a pause or a failed run) are read too. An empty
		outbox reads the whole window."""
		start = max(self.first_day, min(self.first_resent_day, outbox.newest_day or self.first_day))
		recent = {start + timedelta(days=offset) for offset in range((self.today - start).days + 1)}
		return sorted(recent | outbox.pending_days)

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
		if row.status == REFUSED:
			return False
		return row.status == PENDING or line.day >= self.first_resent_day

	def post(self, lines):
		"""Central's reply. Thrown on anything but 200: the lines stay unconfirmed for the next run."""
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
		return CentralReply.parse(response)

	def get_outcomes(self, lines, reply):
		"""{line key: the outbox fields} for each line sent."""
		now = frappe.utils.now_datetime()
		return {
			line.key: {
				"value_sent": line.value,
				"as_of": site_datetime(self.as_of),
				"sent_at": now,
				**reply.get_verdict(line),
			}
			for line in lines
		}


class CentralReply:
	"""Central's answer to one batch. A line is Confirmed only when no rejection could name it: an
	older Central names a rejected line by `subject` alone, so a field it leaves out matches any."""

	def __init__(self, rejections):
		self.rejections_by_subject = {}
		for rejection in map(as_rejection, rejections):
			self.rejections_by_subject.setdefault(rejection.get("subject") or "", []).append(rejection)

	@classmethod
	def parse(cls, response):
		"""Thrown when the reply has no `rejected` list: nothing in the batch can be confirmed."""
		try:
			rejections = response.json()["message"]["rejected"]
		except (ValueError, KeyError, TypeError):
			rejections = None
		if not isinstance(rejections, list):
			frappe.throw(f"Central's reply to the usage report has no rejected list: {response.text[:200]}")
		return cls(rejections)

	def get_rejections(self, line):
		"""Those naming the line's team, or no team, that could name the line."""
		candidates = self.rejections_by_subject.get(line.team, []) + self.rejections_by_subject.get("", [])
		return [rejection for rejection in candidates if could_name(rejection, line)]

	def get_verdict(self, line):
		"""The line's status and reason. Refused only when every rejection that could name the line
		names it exactly and says `retry` false; Pending when any rejection could name it otherwise."""
		rejections = self.get_rejections(line)
		reasons = dict.fromkeys(str(rejection.get("reason") or "") for rejection in rejections)
		return {
			"status": get_status(rejections, line),
			"reason": "; ".join(reason for reason in reasons if reason),
		}


def as_rejection(rejection):
	"""An unreadable `subject` holds every line of the batch, an unreadable `day` every day of its
	subject; neither ever names a line exactly."""
	if not isinstance(rejection, dict):
		return {}
	if not is_readable(rejection.get("subject")):
		return {"reason": rejection.get("reason")}
	if not is_readable(rejection.get("day")):
		return {key: value for key, value in rejection.items() if key not in ("day", "retry")}
	return rejection


def is_readable(value):
	return value is None or isinstance(value, str)


def get_status(rejections, line):
	if not rejections:
		return CONFIRMED
	if all(rejection.get("retry") is False and names_exactly(rejection, line) for rejection in rejections):
		return REFUSED
	return PENDING


def could_name(rejection, line):
	"""A blank `subject` or `day` matches any line; `dimension`, when present, is the model."""
	if rejection.get("subject") and rejection["subject"] != line.team:
		return False
	if rejection.get("day") and rejection["day"] != str(line.day):
		return False
	return "dimension" not in rejection or (rejection["dimension"] or "") == line.model


def names_exactly(rejection, line):
	"""A null `dimension` names the blank model; a missing one names none."""
	return (
		rejection.get("subject") == line.team
		and rejection.get("day") == str(line.day)
		and "dimension" in rejection
		and (rejection["dimension"] or "") == line.model
	)


def sign(secret, body):
	"""base64 HMAC-SHA256 of the raw body, as Central checks it."""
	return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def report_usage():
	"""Tick, hourly. Paused or unconfigured, it sends nothing."""
	UsageReport.load().send()
