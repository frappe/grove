import frappe
from frappe.model.document import Document

from grove.billing.pricing import settle


class GroveCredit(Document):
	"""One entry in a team's credit ledger: a top-up, or a negative correction with a note.
	Append-only — a wrong entry is corrected by another, never edited or deleted — so the ledger
	is the record and `Central Team.balance` is only its sum less `spent`."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		note: DF.Data | None
		reference: DF.Data | None
		team: DF.Link
	# end: auto-generated types

	def before_insert(self):
		# The pull locks the team, then the ledger; the same order here keeps the two from deadlocking.
		frappe.db.get_value("Central Team", self.team, "name", for_update=True)

	def validate(self):
		if not self.amount:
			frappe.throw("0 is not a top-up.")
		if self.amount < 0:
			if not self.note:
				frappe.throw("A negative entry needs a note saying why.")
			self.validate_caps_still_covered()
		before = self.get_doc_before_save()
		if before and any(before.get(field) != self.get(field) for field in ("amount", "team", "reference")):
			frappe.throw("A ledger entry is never edited — add a correcting entry instead.")

	def validate_caps_still_covered(self):
		"""A refund must leave what the live keys' caps still hand out: every store gates on its
		cap, so a balance under the caps is money the keys could spend and the team no longer has."""
		from decimal import Decimal

		from grove.billing.pricing import allocated, spent_by_keys
		from grove.grove.doctype.grove_api_key.grove_api_key import allotted

		left = allocated(self.team) + Decimal(str(self.amount)) - spent_by_keys(self.team)
		handed_out = allotted(self.team)
		if left < handed_out:
			frappe.throw(
				f"This would leave {left:.2f} for caps that hand out {handed_out:.2f}. Lower the keys' caps first."
			)

	def on_update(self):
		# Nothing is sent from here: the next projection push carries the new `budget`.
		settle(self.team)

	def on_trash(self):
		frappe.throw("A ledger entry is never deleted — add a correcting entry instead.")
