# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.access import DEFAULT_LIMITS, LIMIT_TABLES, default_tier, validate_limit_rows
from grove.billing.pricing import forget_figures


class Tier(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.key_limit.key_limit import KeyLimit
		from grove.grove.doctype.model_limit.model_limit import ModelLimit

		key_limits: DF.Table[KeyLimit]
		limit_scope: DF.Literal["Model", "Key"]
		limits: DF.Table[ModelLimit]
		spend_limit: DF.Currency
	# end: auto-generated types

	def validate(self):
		if flt(self.spend_limit) < 0:
			frappe.throw("A spend limit cannot be negative.")
		for table in LIMIT_TABLES.values():
			validate_limit_rows(self.get(table))

	def on_update(self):
		"""Every team on the tier shows its new limits at once: the ones naming it, and the ones
		naming none when it is the default."""
		tiers = [self.name, ""] if self.name == default_tier() else [self.name]
		# ponytail: one cache delete per team; a pattern delete if a tier ever holds a lakh of them.
		for team in frappe.get_all("Grove Team", filters={"tier": ("in", tiers)}, pluck="name"):
			forget_figures(team)


def copied(rows):
	fields = ("model", "metric", "window", "value")
	return [{field: row.get(field) for field in fields if row.get(field) is not None} for row in rows]


def ensure(name):
	"""`name`, inserted as a copy of the default tier when Grove has not seen it: Central names a
	team's tier and the sync never refuses one; the operator tunes the new tier in Desk."""
	if frappe.db.exists("Tier", name):
		return name
	default = default_tier()
	if default:
		source = frappe.get_doc("Tier", default)
	else:
		source = frappe._dict(limits=DEFAULT_LIMITS, key_limits=[])
	frappe.get_doc(
		{
			"doctype": "Tier",
			"__newname": name,
			"spend_limit": flt(source.get("spend_limit")),
			"limit_scope": source.get("limit_scope") or "Model",
			"limits": copied(source.limits),
			"key_limits": copied(source.key_limits),
		}
	).insert()
	return name
