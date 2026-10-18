# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint

from grove.utils import utc_today

RECORD_COLUMNS = {"team": "r.team", "api_key": "r.api_key", "model": "u.model"}
CORRECTION_COLUMNS = {"team": "c.team", "api_key": "c.api_key", "model": "coalesce(c.model, '')"}

RETENTION_DAYS = 90
# ponytail: chunked DELETEs, each its own transaction, so a day of a lakh keys never holds the table
# in one statement; a table partitioned by day would drop a day at once.
PURGE_CHUNK = 10_000


class UsageRecord(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link
		cost: DF.Currency
		day: DF.Date
		drain_id: DF.Data | None
		gateway_cost: DF.Currency
		gateway_store: DF.Link | None
		request_count: DF.Int
		team: DF.Link | None
		total_tokens: DF.Int
		usage: DF.JSON | None
	# end: auto-generated types

	@staticmethod
	def clear_old_logs(days=RETENTION_DAYS):
		"""Log Settings: records served more than `days` UTC days ago, a chunk at a time. One a Credit
		Discrepancy names stays: it is that correction's evidence. No money read needs a record this
		old — `spent` is a running total, the month is this month's, an invoice is a landing window —
		so what goes is history: the usage API and the Revenue report stop at the retention."""
		cutoff = add_days(utc_today(), -cint(days))
		while names := frappe.db.sql_list(
			"""select name from `tabUsage Record` where day < %(cutoff)s
			and name not in (select usage_record from `tabCredit Discrepancy` where usage_record is not null)
			order by day limit %(chunk)s""",
			{"cutoff": cutoff, "chunk": PURGE_CHUNK},
		):
			frappe.db.delete("Usage Record", {"name": ("in", names)})


def on_doctype_update():
	"""A drain is re-sent until acknowledged: the unique makes landing a bucket twice impossible.
	Every read filters a team or a key over a day range."""
	for older in ("unique_drain_key", "unique_drain_key_billed", "unique_drain_key_day_billed"):
		if frappe.db.has_index("tabUsage Record", older):
			frappe.db.sql_ddl(f"alter table `tabUsage Record` drop index {older}")
	frappe.db.add_unique("Usage Record", ["drain_id", "api_key", "day"], constraint_name="unique_drain_key_day")
	frappe.db.add_index("Usage Record", ["team", "day"])
	frappe.db.add_index("Usage Record", ["api_key", "day"])


def tokens_expression(alias="u"):
	"""The SQL for one model entry's whole tokens, `CounterTable.whole` summed."""
	from grove.billing.pricing import CounterTable

	return " + ".join(f"{alias}.{counter}" for counter in CounterTable.load().whole) or "0"


def money_rows(filters, from_date=None, to_date=None, since=None, until=None):
	"""The derived table `x` every money read sums over: one row per model entry of a record
	(team, api_key, model, day, pulled_at, requests, tokens, cost) and one per Usage
	Correction — cost alone, under its model key, on its day — so a correction counts wherever
	usage does, and no record is ever changed. Narrowed by `filters` ({team, api_key, model}: a
	value or a list; None is skipped) and either a UTC day range or a landing window on when a
	record was pulled or a correction landed (site-time datetimes, `until` exclusive). → (sql,
	params)."""
	given = {field: value for field, value in filters.items() if value is not None}
	params = {**given, "from_date": from_date, "to_date": to_date, "since": since, "until": until}

	def where(columns, day, at):
		parts = [
			f"{columns[f]} {'in' if isinstance(v, list | tuple) else '='} %({f})s" for f, v in given.items()
		]
		parts.append(
			f"{at} >= %(since)s and {at} < %(until)s"
			if since
			else f"{day} between %(from_date)s and %(to_date)s"
		)
		return " and ".join(parts)

	# JSON_TABLE's strings carry the JSON column's utf8mb4_bin, so the correction's model is collated
	# to match; one with no model lands under '' rather than being dropped.
	return (
		f"""(select r.team, r.api_key, u.model, r.day, r.creation as pulled_at,
		u.requests, {tokens_expression()} as tokens, u.grove_cost as cost
		from `tabUsage Record` r, {usage_table()} where {where(RECORD_COLUMNS, "r.day", "r.creation")}
		union all
		select c.team, c.api_key, coalesce(c.model, '') collate utf8mb4_bin, c.day, c.creation, 0, 0, c.cost
		from `tabUsage Correction` c where {where(CORRECTION_COLUMNS, "c.day", "c.creation")}) x""",
		params,
	)


def usage_table(record="r"):
	"""The JSON_TABLE that unnests a record's per-model `usage` into rows, one column per counter —
	read off the Usage Counter table, so a new counter needs no SQL edit; an entry from before a
	counter existed reads 0 under it, not NULL. Alias the result `u`."""
	from grove.billing.pricing import CounterTable

	counters = ", ".join(
		f"{counter} bigint path '$.{counter}' default '0' on empty"
		for counter in CounterTable.load().names
		if counter != "request_count"
	)
	return (
		f"json_table({record}.`usage`, '$[*]' columns (model varchar(140) path '$.model', "
		f"pricing varchar(140) path '$.pricing', requests bigint path '$.requests', {counters}, "
		f"grove_cost decimal(21,9) path '$.grove_cost')) u"
	)
