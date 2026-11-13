from decimal import Decimal

import frappe
from frappe.model.document import Document

from grove.utils import utc_today


class CostDiscrepancy(Document):
	"""The gateway charged a drain differently from Grove's price of the same counters at the same
	pricing. Logged by the pull: a person decides which side is wrong and corrects that side's
	spend, which brings the two balances back together. The one the pull decides itself is a dead
	line's, charged by the box at nothing (`Reconciler.audit`). The row keeps the two figures and
	the decision; the money is a record of its own — a Usage Record naming this row when Grove was
	wrong, a Gateway Spend Adjustment when the gateway was — so the Usage Record it audited is never
	changed."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link | None
		delta: DF.Float
		gateway_store: DF.Link | None
		gateway_value: DF.Float
		grove_value: DF.Float
		note: DF.SmallText | None
		pricing: DF.Link | None
		resolution: DF.Literal["", "Grove corrected", "Gateway corrected"]
		resolved_on: DF.Datetime | None
		team: DF.Link | None
		usage_record: DF.Link | None
	# end: auto-generated types

	@frappe.whitelist()
	def correct_grove(self):
		"""Grove priced it wrong: the money lands as a Usage Record of the delta alone, dated today
		and naming this row, which every money read sums from then on, so Grove's spend meets the
		gateway's. One entry under the pricing's model: no requests, no tokens."""
		from grove.billing.pricing import lock_team

		self.check_open()
		lock_team(self.team)
		entry = {
			"model": model_key_of(self.pricing),
			"pricing": self.pricing,
			"requests": 0,
			"grove_cost": self.delta,
		}
		frappe.get_doc(
			{
				"doctype": "Usage Record",
				"cost_discrepancy": self.name,
				"team": self.team,
				"api_key": self.api_key,
				"day": utc_today(),
				"cost": self.delta,
				"usage": frappe.as_json([entry], indent=None),
			}
		).insert()
		self.decide("Grove corrected")

	@frappe.whitelist()
	def correct_gateway(self):
		"""The gateway charged it wrong."""
		self.check_open()
		self.adjust_gateway()

	def adjust_gateway(self):
		"""The gateway's spend counter for the key on that store is owed minus the delta. Only
		recorded here, as a Pending Gateway Spend Adjustment — the next projection tick sends it, and
		every tick after until the box answers. Grove's side does not move. The pull calls this for
		a dead line under whoever pulled, so the row is written past permissions."""
		if not self.gateway_store:
			frappe.throw("This drain names no store, so there is nothing to send the correction through.")
		frappe.get_doc(
			{
				"doctype": "Gateway Spend Adjustment",
				"cost_discrepancy": self.name,
				"team": self.team,
				"api_key": self.api_key,
				"gateway_store": self.gateway_store,
				"adjustment": -float(self.delta),
			}
		).insert(ignore_permissions=True)
		self.decide("Gateway corrected")

	def check_open(self):
		frappe.only_for("System Manager")
		if self.resolution:
			frappe.throw(f"This discrepancy is already decided: {self.resolution}.")

	def decide(self, resolution):
		self.db_set({"resolution": resolution, "resolved_on": frappe.utils.now_datetime()})


def model_key_of(pricing):
	"""The model key a pricing prices, what a money read groups its correction under. "" for a row
	that names none: a discrepancy from before pricings were tagged."""
	if not pricing:
		return ""
	model = frappe.db.get_value("Model Pricing", pricing, "model")
	return frappe.db.get_value("Model", model, "model_key") or ""


def record(**facts):
	"""The one writer: one row per (Usage Record, pricing) the gateway charged differently."""
	facts = {k: float(v) if isinstance(v, Decimal) else v for k, v in facts.items()}
	return frappe.get_doc({"doctype": "Cost Discrepancy", **facts}).insert(ignore_permissions=True)
