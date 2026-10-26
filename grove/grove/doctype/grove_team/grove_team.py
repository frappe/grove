# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.billing.pricing import forget_figures


class GroveTeam(Document):
	"""A control client's team — Central's (`TEAM-00520`) or Press's (`Press Team`) — named by the
	client's id for it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		key_cap_window: DF.Literal["Monthly", "Total"]
		last_reported_at: DF.Datetime | None
		log_payloads: DF.Check
		max_keys: DF.Int
		override_max_keys: DF.Check
		suspended: DF.Check
		suspended_at_grove: DF.Check
		tier: DF.Link | None
	# end: auto-generated types

	def validate(self):
		if (self.max_keys or 0) < 0:
			frappe.throw("Max Keys cannot be negative; 0 is no limit.")
		# Central's sync writes `suspended` as it sees the team; Grove's hold outranks it.
		if self.suspended_at_grove:
			self.suspended = 1

	def on_update(self):
		forget_figures(self.name)

	@frappe.whitelist()
	def set_suspended_at_grove(self, suspended: bool):
		"""Desk's Suspend Keys / Resume Keys: suspends the team as Central would, marked as Grove's
		own so Central's sync cannot lift it; the next tick has the gateways refuse every key, cap or
		no cap, and nothing is revoked. Resume clears both — Central puts its own back on its next
		sync if the invoice is still unpaid."""
		self.suspended_at_grove = self.suspended = suspended
		self.save()

	@property
	def active_keys(self):
		return frappe.db.count("Grove API Key", {"team": self.name, "status": "active"})
