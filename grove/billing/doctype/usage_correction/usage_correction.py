import frappe
from frappe.model.document import Document


class UsageCorrection(Document):
	"""The money of a Credit Discrepancy resolved as Grove's error: what the key's charge moved by,
	under the pricing's model, on the UTC day it was resolved. Written once by
	`CreditDiscrepancy.correct_grove` and never changed; every money read (`money_rows`,
	`month_spent_by`) sums it beside the Usage Records, so no record is ever edited. The decision
	itself stays on the Credit Discrepancy."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link
		cost: DF.Currency
		credit_discrepancy: DF.Link
		day: DF.Date
		model: DF.Data | None
		pricing: DF.Link | None
		team: DF.Link
	# end: auto-generated types


def on_doctype_update():
	"""Every money read filters a team or a key over a day range, as it reads the records."""
	frappe.db.add_index("Usage Correction", ["team", "day"])
	frappe.db.add_index("Usage Correction", ["api_key", "day"])
