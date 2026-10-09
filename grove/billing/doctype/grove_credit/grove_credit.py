from decimal import ROUND_DOWN, Decimal

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.billing.pricing import allocated, settle, spent_by_keys
from grove.grove.doctype.grove_api_key.grove_api_key import allotted

NANO = Decimal("0.000000001")


class GroveCredit(Document):
	"""One entry in a team's credit ledger: a top-up, or a negative correction with a note.
	Append-only — a wrong entry is corrected by another, never edited or deleted — so the ledger
	is the record and `Central Team.balance` is only its sum less `spent`. A top-up is handed to
	the live keys' caps: its rows say how, none means in proportion to the caps."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.billing.doctype.grove_credit_allocation.grove_credit_allocation import (
			GroveCreditAllocation,
		)

		allocations: DF.Table[GroveCreditAllocation]
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
		if before and (before.amount, before.team, before.reference, rows_of(before)) != (self.amount, self.team, self.reference, rows_of(self)):
			frappe.throw("A ledger entry is never edited — add a correcting entry instead.")
		if self.is_new():
			self.validate_allocations()

	def validate_caps_still_covered(self):
		"""A refund must leave what the live keys' caps still hand out: every store gates on its
		cap, so a balance under the caps is money the keys could spend and the team no longer has."""
		left = self.balance_after
		handed_out = allotted(self.team)
		if left < handed_out:
			frappe.throw(
				f"This would leave {left:.2f} for caps that hand out {handed_out:.2f}. Lower the keys' caps first."
			)

	def validate_allocations(self):
		"""Rows name live keys of the team, once each, with something to hand out, and together
		no more than this top-up leaves free. None on a prepaid top-up: filled from the caps."""
		if self.amount < 0 or frappe.db.get_value("Central Team", self.team, "free"):
			if self.allocations:
				frappe.throw("Only a top-up of a prepaid team is handed to keys.")
			return
		live = self.live_keys
		if not self.allocations:
			self.fill_allocations(live)
			return
		seen = set()
		for row in self.allocations:
			if row.api_key not in live or row.api_key in seen:
				frappe.throw(f"Row {row.idx}: {row.api_key} is not a live key of {self.team}, or is named twice.")
			if flt(row.amount) <= 0:
				frappe.throw(f"Row {row.idx}: nothing to hand to {row.api_key}.")
			seen.add(row.api_key)
		if (total := sum(Decimal(str(row.amount)) for row in self.allocations)) > self.spread:
			frappe.throw(f"These rows hand out {total:.2f} of a top-up that leaves {self.spread:.2f} to hand out.")

	def fill_allocations(self, live):
		"""Each key's share of the top-up in proportion to its cap, to the nano, the rounding
		remainder on the largest. A key with no cap yet takes no share: it has not been given one."""
		spread = self.spread
		weights = {name: Decimal(str(key.cap or 0)) for name, key in live.items()}
		if spread <= 0 or not (total := sum(weights.values())):
			return
		shares = {name: (spread * weight / total).quantize(NANO, rounding=ROUND_DOWN) for name, weight in weights.items()}
		first = next(iter(shares))
		shares[first] += spread - sum(shares.values())
		for name, share in shares.items():
			if share > 0:
				self.append("allocations", {"api_key": name, "amount": share})

	@property
	def live_keys(self):
		"""The team's active keys by name, with their cap and spent, largest cap first."""
		rows = frappe.get_all(
			"Grove API Key", filters={"team": self.team, "status": "active"},
			fields=["name", "cap", "spent"], order_by="cap desc, creation asc",
		)
		return {row.name: row for row in rows}

	@property
	def balance_after(self):
		return allocated(self.team) + Decimal(str(self.amount)) - spent_by_keys(self.team)

	@property
	def spread(self):
		"""What this top-up may hand out: itself, less what a debt or caps already past the
		balance swallow first."""
		return max(Decimal(0), min(Decimal(str(self.amount)), self.balance_after - allotted(self.team)))

	def after_insert(self):
		# Nothing is sent from here: the next projection push carries each key's new `budget`.
		settle(self.team)
		for row in self.allocations:
			key = frappe.get_doc("Grove API Key", row.api_key)
			# From what it spent when it overshot: the row is what the key may spend from here.
			key.cap = flt(max(flt(key.cap), flt(key.spent)) + flt(row.amount), 9)
			key.save(ignore_permissions=True)

	def on_trash(self):
		frappe.throw("A ledger entry is never deleted — add a correcting entry instead.")


def rows_of(doc):
	return [(row.api_key, flt(row.amount, 9)) for row in doc.allocations]
