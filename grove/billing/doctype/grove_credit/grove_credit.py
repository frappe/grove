import frappe
from frappe.model.document import Document

from grove.billing.pricing import settle


class GroveCredit(Document):
	"""One entry in a user's credit ledger: a top-up, or a negative correction with a note.
	Append-only — a wrong entry is corrected by another, never edited or deleted — so the ledger
	is the record and `Grove User.balance` is only its sum less `spent`."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		amount: DF.Currency
		grove_user: DF.Link
		note: DF.Data | None
		reference: DF.Data | None
	# end: auto-generated types

	def before_insert(self):
		# The pull locks the user, then the ledger; the same order here keeps the two from deadlocking.
		frappe.db.get_value("Grove User", self.grove_user, "name", for_update=True)

	def validate(self):
		if not self.amount:
			frappe.throw("0 is not a top-up.")
		if self.amount < 0 and not self.note:
			frappe.throw("A negative entry needs a note saying why.")
		before = self.get_doc_before_save()
		if before and any(before.get(field) != self.get(field) for field in ("amount", "grove_user", "reference")):
			frappe.throw("A ledger entry is never edited — add a correcting entry instead.")

	def on_update(self):
		# Nothing is sent from here: the next projection push carries the new `budget`.
		settle(self.grove_user)

	def on_trash(self):
		frappe.throw("A ledger entry is never deleted — add a correcting entry instead.")
