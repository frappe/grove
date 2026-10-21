# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from grove.billing.pricing import forget_figures


class GroveTeam(Document):
	"""A control client's team — Central's (`TEAM-00520`) or Press's (`Press Team`) — named by the
	client's id for it. Policy — models, geography, rate limits, the cap — sits on each Grove API Key; what is
	the team's is what its `tier` allows (`max_keys`, `spend_limit`, the rate limits its unpinned
	keys run under; its own `max_keys` under Override Max Keys), what its keys' caps count
	(`key_cap_window`: the month, or everything since the mint) and whether it is suspended. What
	its keys spent is the Usage Records'; who the team is, its contacts, credit and invoices are
	the client's.

	The gateway holds no team record: `log_payloads` and `suspended` ride on every key of the
	team, so suspending it refuses every key with one field each."""

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
		tier: DF.Link | None
	# end: auto-generated types

	def validate(self):
		if (self.max_keys or 0) < 0:
			frappe.throw("Max Keys cannot be negative; 0 is no limit.")

	def on_update(self):
		forget_figures(self.name)

	@property
	def active_keys(self):
		return frappe.db.count("Grove API Key", {"team": self.name, "status": "active"})
