# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.access import validate_model_geography

DEFAULT_LIMITS = (
	{"metric": "requests", "window": "1m", "value": 20},
	{"metric": "total_tokens", "window": "1m", "value": 20_000},
)


class GroveUser(Document):
	"""Grove's per-user policy: their groups, which models they may call, and their prepaid
	balance (every user has one unless marked Free). All of it belongs to the USER — whatever the
	control client's `reference` names, a team in Central — and their keys are credentials that
	share this balance. `email` is where their alerts go, not a login.
	No doc means no group and no allow, so the user reaches no models at all. A new doc starts in
	their geography's default Model Group, when one is marked, and under the default rate limits.

	The gateway holds it the same way: one user:<name> record every key points at, so an access
	change is a single write however many keys they hold."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow
		from grove.grove.doctype.model_group_row.model_group_row import ModelGroupRow
		from grove.grove.doctype.model_limit.model_limit import ModelLimit

		allow: DF.Table[GroveModelRow]
		balance: DF.Currency
		credit_exhausted: DF.Check
		deny: DF.Table[GroveModelRow]
		email: DF.Data
		free: DF.Check
		geography: DF.Link
		limits: DF.Table[ModelLimit]
		log_payloads: DF.Check
		model_groups: DF.TableMultiSelect[ModelGroupRow]
		reference: DF.Data | None
		spent: DF.Currency
	# end: auto-generated types

	def before_insert(self):
		"""A new user starts in their geography's default Model Group and under DEFAULT_LIMITS,
		unless the insert names its own groups or limits."""
		self.set_geography()
		default = frappe.db.get_value("Model Group", {"is_default": 1, "geography": self.geography})
		if default and not self.model_groups:
			self.append("model_groups", {"model_group": default})
		if not self.limits:
			for limit in DEFAULT_LIMITS:
				self.append("limits", dict(limit))


	def set_geography(self):
		"""One geography per user: blank is the default one."""
		if not self.geography:
			self.geography = frappe.db.get_value("Geography", {"is_default": 1})
		if not self.geography:
			frappe.throw("Mark one Geography as default, or pick a geography for this user.")

	def validate(self):
		validate_model_geography([*self.allow, *self.deny], self.geography)
		# Deny wins anyway, so a model on both lists is a mistake worth surfacing.
		both = {row.model_key for row in self.allow} & {row.model_key for row in self.deny}
		if both:
			frappe.throw(f"{', '.join(sorted(both))} is in both Allow and Deny")
		self.validate_limits()
		if not self.is_new():
			# save() writes every column and the pull writes these two behind the form's back, so a
			# form left open across a pull would write its old totals back as free credit.
			live = frappe.db.get_value("Grove User", self.name, ["spent", "balance"], as_dict=True, for_update=True)
			self.spent, self.balance = live.spent, live.balance
		# Mirrors pricing.settle, which on_update then runs for real: free is never gated.
		self.credit_exhausted = int(not self.free and (self.balance or 0) <= 0)

	def validate_limits(self):
		"""One limit per metric and window; the gateway refuses a push carrying one it cannot read."""
		seen = set()
		for row in self.limits:
			if (row.value or 0) <= 0:
				frappe.throw(f"Limit row {row.idx}: the value must be above zero.")
			if (row.metric, row.window) in seen:
				frappe.throw(f"Two limits on {row.metric} per {row.window}.")
			seen.add((row.metric, row.window))

	def on_update(self):
		"""The verdict is re-decided from what was just saved — the same writer the pull uses."""
		from grove.billing.pricing import settle

		settle(self.name)


def for_reference(reference):
	"""Only the outward-facing edges speak the control client's reference; everything downstream
	of a key carries this name."""
	return frappe.db.get_value("Grove User", {"reference": reference}) if reference else None


def set_credit_exhausted(grove_user, exhausted):
	"""Flip the credit gate for `grove_user`. Held here, not on the keys, because the balance is
	the user's — storing it per key let a blocked user mint a fresh one and walk past their
	own cap. The next sync pushes one record, not one per key they hold.
	Returns True when something actually changed."""
	current = frappe.db.get_value("Grove User", grove_user, "credit_exhausted")
	if current is None or current == int(exhausted):
		return False
	# update_modified=False: a system flag flip is not a user edit and must not read as one.
	frappe.db.set_value("Grove User", grove_user, "credit_exhausted", int(exhausted), update_modified=False)
	return True
