# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from grove.access import team_limits
from grove.billing.pricing import forget_figures


class CentralTeam(Document):
	"""A team in Central, named by its id there (`TEAM-00520`).
	Policy — models, geography, rate limits, the monthly cap — sits on each Grove API Key; what is
	the team's is what its `tier` allows (`max_keys`, `spend_limit`, the rate limits its unpinned
	keys run under; its own `max_keys` under Override Max Keys), whether it is Free or suspended,
	and what its keys spent. Who the team is, its contacts, credit and invoices are Central's.

	The gateway holds no team record: `free`, `log_payloads` and `suspended` ride on every key of
	the team, so suspending it refuses every key with one field each."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		free: DF.Check
		last_reported_at: DF.Datetime | None
		log_payloads: DF.Check
		max_keys: DF.Int
		override_max_keys: DF.Check
		spent: DF.Currency
		suspended: DF.Check
		tier: DF.Link | None
	# end: auto-generated types

	def validate(self):
		if (self.max_keys or 0) < 0:
			frappe.throw("Max Keys cannot be negative; 0 is no limit.")
		if not self.is_new():
			self.spent = frappe.db.get_value("Central Team", self.name, "spent", for_update=True)

	def on_update(self):
		forget_figures(self.name)
		before = self.get_doc_before_save()
		if before and before.free and not self.free:
			self.reset_caps()

	def reset_caps(self):
		"""Caps are not checked while a team is Free, so turning Free off starts every live key at
		0: each is capped again within the spend limit, never at a number nobody vetted."""
		for key in self.live_keys:
			frappe.db.set_value("Grove API Key", key, "cap", 0)

	@property
	def live_keys(self):
		return frappe.get_all("Grove API Key", filters={"team": self.name, "status": "active"}, pluck="name")

	@frappe.whitelist()
	def set_free(self, free: bool | int | str, caps: list[dict] | None = None):
		"""Button. Free: the team stops being charged. Paid: every live key gets the monthly cap
		`caps` names for it ({key, cap}), 0 when left out — refused until it gets one — checked
		against the spend limit before anything is written, so a refusal leaves the team as it was."""
		frappe.only_for("System Manager")
		self.free = int(bool(frappe.utils.sbool(free)))
		if self.free:
			self.save()
			return
		caps = {row["key"]: flt(row.get("cap")) for row in (caps or [])}
		pending = self.check_caps(caps)
		self.save()
		for key in self.live_keys:
			doc = frappe.get_doc("Grove API Key", key)
			doc.cap = caps.get(key, 0)
			doc.flags.pending_spend = pending
			doc.save()

	def check_caps(self, caps):
		"""The caps and this month's spend together within the spend limit (`pricing.committed`,
		with what the boxes hold this moment) — what each key's own validate will insist on, said
		once up front. → what the boxes hold, for those saves."""
		from grove.billing.pricing import committed, usd
		from grove.pathway.usage import peek_team

		pending = peek_team(self.name)
		spend_limit = flt(team_limits(self.name).spend_limit, 9)
		total = committed(self.name, {key: caps.get(key, 0) for key in self.live_keys}, pending=pending)
		if total > spend_limit:
			frappe.throw(
				f"This month's spend and these caps add up to {usd(total)} of a spend limit of {usd(spend_limit)}."
			)
		return pending

	@property
	def active_keys(self):
		return frappe.db.count("Grove API Key", {"team": self.name, "status": "active"})
