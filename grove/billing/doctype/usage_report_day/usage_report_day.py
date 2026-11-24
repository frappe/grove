# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname

DOCTYPE = "Usage Report Day"
OUTCOME_FIELDS = ("value_sent", "as_of", "sent_at", "status", "confirmed_at", "reason")
PENDING, CONFIRMED, REFUSED = "Pending", "Confirmed", "Refused"


class UsageReportDay(Document):
	"""The outbox of the usage report: what Grove last sent Central for one team, model and UTC
	landing day, and whether Central confirmed it. The value itself is always recomputed from the
	Usage Records; this row only says whether the day still has to be sent."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		as_of: DF.Datetime | None
		confirmed_at: DF.Datetime | None
		day: DF.Date
		model: DF.Data | None
		reason: DF.SmallText | None
		sent_at: DF.Datetime | None
		status: DF.Literal["Pending", "Confirmed", "Refused"]
		team: DF.Link
		value_sent: DF.LongInt
	# end: auto-generated types


def on_doctype_update():
	frappe.db.add_unique(DOCTYPE, ["team", "model", "day"], constraint_name="unique_team_model_day")


def report_key(team, model, day):
	"""A report line's identity: a blank model is "", the day an ISO date."""
	return (team, model or "", str(day))


def get_status(rejection):
	"""A line Central did not reject is Confirmed; a rejected one is Pending if Central asks for it
	again, Refused if not."""
	if not rejection:
		return CONFIRMED
	return PENDING if rejection.get("retry") else REFUSED


class UsageReportDays:
	"""The outbox rows of the landing days the report still sends, keyed like its lines. Rows of
	days before `first_day` are dropped on load."""

	def __init__(self, first_day):
		frappe.db.delete(DOCTYPE, {"day": ("<", first_day)})
		rows = frappe.get_all(DOCTYPE, fields=["name", "team", "model", "day", "value_sent", "status"])
		self.rows = {report_key(row.team, row.model, row.day): row for row in rows}

	def get(self, key):
		return self.rows.get(key)

	def record(self, outcomes):
		"""Writes each line's outcome, {key: fields}: a known day in place, a new one inserted."""
		new = []
		for key, fields in outcomes.items():
			if row := self.rows.get(key):
				frappe.db.set_value(DOCTYPE, row.name, fields, update_modified=False)
				row.update(fields)
			else:
				new.append(new_row(key, fields))
				self.rows[key] = frappe._dict(new[-1])
		if new:
			frappe.db.bulk_insert(DOCTYPE, list(new[0]), [list(row.values()) for row in new])


def new_row(key, fields):
	team, model, day = key
	now = frappe.utils.now()
	return {
		"name": make_autoname("hash", DOCTYPE),
		"creation": now,
		"modified": now,
		"owner": frappe.session.user,
		"modified_by": frappe.session.user,
		"team": team,
		"model": model,
		"day": day,
		**{field: fields[field] for field in OUTCOME_FIELDS},
	}
