# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class UsageRecord(Document):
	"""One API key's usage in one drain of one store. Inserted by the pull and never updated: `cost`
	is Grove's price of it, charged to the user when `billed` (they were not Free as it landed);
	`usage` holds the per-model detail as JSON, one entry per model, so a record is one row however
	many models it touched."""

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
		usage: DF.JSON | None
		user: DF.Link | None
	# end: auto-generated types

	pass


def on_doctype_update():
	"""A drain is re-sent until acknowledged: the unique pair makes landing it twice impossible.
	Every read filters a user or a key over a day range."""
	frappe.db.add_unique("Usage Record", ["drain_id", "api_key"], constraint_name="unique_drain_key")
	frappe.db.add_index("Usage Record", ["user", "day"])
	frappe.db.add_index("Usage Record", ["api_key", "day"])


def usage_table(record="r"):
	"""The JSON_TABLE that unnests a record's per-model `usage` into rows, one column per counter —
	read off the Usage Counter table, so a new counter needs no SQL edit. Alias the result `u`."""
	from grove.pricing import CounterTable

	counters = ", ".join(
		f"{counter} bigint path '$.{counter}'" for counter in CounterTable.load().names if counter != "request_count"
	)
	return (
		f"json_table({record}.`usage`, '$[*]' columns (model varchar(140) path '$.model', "
		f"pricing varchar(140) path '$.pricing', requests bigint path '$.requests', {counters}, "
		f"grove_cost decimal(21,9) path '$.grove_cost')) u"
	)
