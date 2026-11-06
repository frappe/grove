# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.access import DEFAULT_LIMITS, default_tier, validate_limit_rows


class Tier(Document):
	"""What a team on it may do, named by Central's trust tier label (`Growth`): monthly spend
	across its keys' caps, and the rate limits the team runs under per model (a blank row is every
	model, a named one that model alone; no rows: uncapped). Central names a team's tier, and
	renames it when the label changes (`api.teams.rename_tier`). `spend_limit` is tuned in Desk;
	the rows are the catalog's (`catalog.seed.sync_tier`). A tier a team names cannot be deleted."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.model_limit.model_limit import ModelLimit

		limits: DF.Table[ModelLimit]
		spend_limit: DF.Currency
	# end: auto-generated types

	def validate(self):
		if flt(self.spend_limit) < 0:
			frappe.throw("A spend limit cannot be negative.")
		validate_limit_rows(self.limits)


def ensure(name):
	"""`name`, inserted as a copy of the default tier when Grove has not seen it: Central names a
	team's tier and the sync never refuses one; the operator tunes the new tier in Desk."""
	if not frappe.db.exists("Tier", name):
		source = (
			frappe.get_doc("Tier", default)
			if (default := default_tier())
			else frappe._dict(limits=DEFAULT_LIMITS)
		)
		frappe.get_doc(
			{
				"doctype": "Tier",
				"__newname": name,
				"spend_limit": flt(source.get("spend_limit")),
				"limits": [
					{
						"model_key": r.get("model_key") or "",
						"metric": r.get("metric"),
						"window": r.get("window"),
						"value": r.get("value"),
					}
					for r in source.limits
				],
			}
		).insert()
	return name
