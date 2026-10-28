# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.billing.pricing import forget_figures


class GroveTeam(Document):
	"""A control client's team — Central's (`TEAM-00520`) or Press's (`Press Team`) — named by the
	client's id for it. What its keys may call is decided here, not on the key: a key is revoked and
	minted again at will, the team is not. A Model Group and a vendor model are one geography's and
	reach only the keys there; a model of ours reaches every key."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.grove_model_row.grove_model_row import GroveModelRow
		from grove.grove.doctype.model_group_row.model_group_row import ModelGroupRow

		allow: DF.TableMultiSelect[GroveModelRow]
		deny: DF.TableMultiSelect[GroveModelRow]
		key_cap_window: DF.Literal["Monthly", "Total"]
		last_reported_at: DF.Datetime | None
		log_payloads: DF.Check
		max_keys: DF.Int
		model_groups: DF.TableMultiSelect[ModelGroupRow]
		override_max_keys: DF.Check
		suspended: DF.Check
		suspended_at_grove: DF.Check
		tier: DF.Link | None
	# end: auto-generated types

	def before_insert(self):
		"""A new team starts in every geography's default Model Group unless the insert names its
		own groups."""
		if not self.model_groups:
			defaults = frappe.get_all("Model Group", filters={"is_default": 1}, pluck="name", order_by="name")
			for group in defaults:
				self.append("model_groups", {"model_group": group})

	def validate(self):
		if (self.max_keys or 0) < 0:
			frappe.throw("Max Keys cannot be negative; 0 is no limit.")
		# By doc, not key: one vendor key is a doc per geography, allowed in one and denied in another.
		denied = {row.model for row in self.deny}
		both = sorted(row.model_key for row in self.allow if row.model in denied)
		if both:
			frappe.throw(f"{', '.join(both)} is in both Allow and Deny")
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
