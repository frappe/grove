# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CentralTeam(Document):
	"""A team in Central, named by its id there (`TEAM-00520`): the ledger every key of the team
	spends out of.
	Policy — models, geography, rate limits, the cap — sits on each Grove API Key; what is the
	team's is the money (Σ Grove Credit less what its keys spent), whether it is Free, how many
	keys it may hold, and the address its alerts go to.

	The gateway holds no team record: `free`, `log_payloads` and `credit_exhausted` ride on every
	key of the team, so the team running out refuses every key with one field each."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		balance: DF.Currency
		credit_exhausted: DF.Check
		email: DF.Data
		free: DF.Check
		log_payloads: DF.Check
		max_keys: DF.Int
		spent: DF.Currency
	# end: auto-generated types

	def validate(self):
		if (self.max_keys or 0) <= 0:
			frappe.throw("Max Keys must be above zero.")
		if not self.is_new():
			# save() writes every column and the pull writes these two behind the form's back, so a
			# form left open across a pull would write its old totals back as free credit.
			live = frappe.db.get_value("Central Team", self.name, ["spent", "balance"], as_dict=True, for_update=True)
			self.spent, self.balance = live.spent, live.balance
		# Mirrors pricing.settle, which on_update then runs for real: free is never gated.
		self.credit_exhausted = int(not self.free and (self.balance or 0) <= 0)

	def on_update(self):
		"""The verdict is re-decided from what was just saved — the same writer the pull uses."""
		from grove.billing.pricing import settle

		before = self.get_doc_before_save()
		if before and before.free and not self.free:
			self.reset_caps()
		settle(self.name)

	def reset_caps(self):
		"""Caps are not checked while a team is Free, so turning Free off starts every live key at
		0: each is capped again out of what the team loads, never out of a number nobody vetted."""
		live = frappe.get_all("Grove API Key", filters={"team": self.name, "status": "active"}, pluck="name")
		for key in live:
			frappe.db.set_value("Grove API Key", key, "cap", 0, update_modified=False)
		if live:
			frappe.msgprint(f"{len(live)} live keys reset to a cap of 0. Set their caps out of what the team loads.")

	@property
	def active_keys(self):
		return frappe.db.count("Grove API Key", {"team": self.name, "status": "active"})


def set_credit_exhausted(team, exhausted):
	"""Flip the credit gate for `team`. Held here, not on the keys, because the balance is the
	team's; the next sync pushes it as `limited` on each key the team holds. Returns True when
	something actually changed."""
	current = frappe.db.get_value("Central Team", team, "credit_exhausted")
	if current is None or current == int(exhausted):
		return False
	# update_modified=False: a system flag flip is not a user edit and must not read as one.
	frappe.db.set_value("Central Team", team, "credit_exhausted", int(exhausted), update_modified=False)
	return True
