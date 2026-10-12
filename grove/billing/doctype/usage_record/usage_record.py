# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# The same logical filter on each half of `money_rows`.
RECORD_COLUMNS = {"team": "r.team", "api_key": "r.api_key", "model": "u.model"}
CORRECTION_COLUMNS = {"team": "d.team", "api_key": "d.api_key", "model": "m.model_key"}


class UsageRecord(Document):
	"""One API key's usage in one drain of one store, billed and free apart. Inserted by the pull and
	never updated: `cost` is Grove's price of it, charged to the key when `billed` (the gateway
	served it while its team was prepaid); `usage` holds the per-model detail as JSON, one entry per
	model, so a record is one row however many models it touched. A correction is never written
	here: a Credit Discrepancy Grove resolved is folded in by `money_rows`."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link
		billed: DF.Check
		cost: DF.Currency
		day: DF.Date
		drain_id: DF.Data | None
		gateway_cost: DF.Currency
		gateway_store: DF.Link | None
		request_count: DF.Int
		team: DF.Link | None
		usage: DF.JSON | None
	# end: auto-generated types

	pass


def on_doctype_update():
	"""A drain is re-sent until acknowledged: the unique triple makes landing it twice impossible.
	Every read filters a team or a key over a day range."""
	# The pair it replaces: a key's billed and free usage in one drain are two records.
	if frappe.db.has_index("tabUsage Record", "unique_drain_key"):
		frappe.db.sql_ddl("alter table `tabUsage Record` drop index unique_drain_key")
	frappe.db.add_unique("Usage Record", ["drain_id", "api_key", "billed"], constraint_name="unique_drain_key_billed")
	frappe.db.add_index("Usage Record", ["team", "day"])
	frappe.db.add_index("Usage Record", ["api_key", "day"])


def tokens_expression(alias="u"):
	"""The SQL for a row's whole tokens: the root token counters that are not a part of another,
	so prompt and completion once each and never their cached or audio parts again."""
	from grove.billing.pricing import CounterTable

	whole = [
		row.name
		for row in CounterTable.load().rows
		if row.unit == "Mtok" and not row.base_counter and not row.part_of
	]
	return " + ".join(f"{alias}.{counter}" for counter in whole) or "0"


def money_rows(filters, from_date=None, to_date=None, since=None, until=None):
	"""The derived table `x` every money read sums over: one row per model entry of a record
	(team, api_key, model, day, pulled_at, billed, requests, tokens, cost) and one per correction
	of a Credit Discrepancy Grove resolved — cost alone, under the pricing's model (blank for a
	row that names none), on the UTC day it was resolved — so a correction counts wherever the
	usage it corrects does, and the record itself is never changed. Narrowed by `filters` ({team, api_key, model}: a value or a list;
	None is skipped) and either a UTC day range or a landing window on when a record was pulled
	or a correction resolved (site-time datetimes, `until` exclusive). → (sql, params)."""
	given = {field: value for field, value in filters.items() if value is not None}
	params = {**given, "from_date": from_date, "to_date": to_date, "since": since, "until": until}

	def where(columns, day, at):
		parts = [f"{columns[f]} {'in' if isinstance(v, list | tuple) else '='} %({f})s" for f, v in given.items()]
		parts.append(f"{at} >= %(since)s and {at} < %(until)s" if since else f"{day} between %(from_date)s and %(to_date)s")
		return " and ".join(parts)

	# A model key is exact, as the gateway's buckets name it: JSON_TABLE's strings carry the JSON
	# column's utf8mb4_bin, and the correction side is made to match. A correction is never dropped
	# for lacking a pricing: it lands under a blank model.
	return (
		f"""(select r.team, r.api_key, u.model, r.day, r.creation as pulled_at, r.billed,
		u.requests, {tokens_expression()} as tokens, if(r.billed, u.grove_cost, 0) as cost
		from `tabUsage Record` r, {usage_table()} where {where(RECORD_COLUMNS, "r.day", "r.creation")}
		union all
		select d.team, d.api_key, coalesce(m.model_key, '') collate utf8mb4_bin, d.resolved_day, d.resolved_on, 1,
		0, 0, d.correction
		from `tabCredit Discrepancy` d
		left join `tabModel Pricing` p on p.name = d.pricing left join `tabModel` m on m.name = p.model
		where d.resolution = 'Grove corrected' and {where(CORRECTION_COLUMNS, "d.resolved_day", "d.resolved_on")}) x""",
		params,
	)


def usage_table(record="r"):
	"""The JSON_TABLE that unnests a record's per-model `usage` into rows, one column per counter —
	read off the Usage Counter table, so a new counter needs no SQL edit. Alias the result `u`."""
	from grove.billing.pricing import CounterTable

	counters = ", ".join(
		f"{counter} bigint path '$.{counter}'" for counter in CounterTable.load().names if counter != "request_count"
	)
	return (
		f"json_table({record}.`usage`, '$[*]' columns (model varchar(140) path '$.model', "
		f"pricing varchar(140) path '$.pricing', requests bigint path '$.requests', {counters}, "
		f"grove_cost decimal(21,9) path '$.grove_cost')) u"
	)
