from decimal import Decimal

import frappe
from frappe.model.document import Document


class CreditDiscrepancy(Document):
	"""The gateway charged a drain differently from Grove's price of the same counters at the same
	pricing. Logged by the pull and never acted on by it: a person decides which side is wrong and
	corrects that side's spend, which brings the two balances back together."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Link | None
		correction: DF.Currency
		delta: DF.Currency
		gateway_correction_pending: DF.Check
		gateway_spent: DF.Currency
		gateway_store: DF.Link | None
		gateway_value: DF.Currency
		grove_user: DF.Link | None
		grove_value: DF.Currency
		note: DF.SmallText | None
		pricing: DF.Link | None
		resolution: DF.Literal['', 'Grove corrected', 'Gateway corrected']
		usage_record: DF.Link | None
	# end: auto-generated types

	@frappe.whitelist()
	def correct_grove(self):
		"""Grove priced it wrong: `spent` moves by the delta and the user is settled, so Grove's
		balance meets the gateway's."""
		from grove.billing.pricing import settle

		self.check_open()
		delta = Decimal(str(self.delta))
		frappe.db.sql("update `tabGrove User` set spent = spent + %s where name = %s", [delta, self.grove_user])
		settle(self.grove_user)
		self.db_set({"correction": delta, "resolution": "Grove corrected"})

	@frappe.whitelist()
	def correct_gateway(self):
		"""The gateway charged it wrong: its spend counter for the user on that store is owed minus
		the delta. Only recorded here — the next projection tick sends it, and every tick after until
		the box answers. Grove's side can no longer be corrected."""
		self.check_open()
		if not self.gateway_store:
			frappe.throw("This drain names no store, so there is nothing to send the correction through.")
		self.db_set("gateway_correction_pending", 1)

	def check_open(self):
		frappe.only_for("System Manager")
		if self.resolution or self.gateway_correction_pending:
			frappe.throw(f"This discrepancy is already decided: {self.resolution or 'waiting on the gateway'}.")


def pending_adjustments():
	"""{store: [spend-adjust body]} for every correction no gateway has answered. The box applies
	each once under the row's name, so sending again is safe."""
	from grove.billing.pricing import nano

	# ponytail: the box forgets an id after 7 days, so a row pending longer could apply twice.
	# Keep ids forever in pathway if one ever does.
	rows = frappe.get_all(
		"Credit Discrepancy", filters={"gateway_correction_pending": 1},
		fields=["name", "grove_user", "gateway_store", "delta"], order_by="creation asc",
	)
	owed = {}
	for row in rows:
		body = {"user": row.grove_user, "delta": -nano(row.delta), "id": row.name}
		owed.setdefault(row.gateway_store, []).append(body)
	return owed


def mark_corrected(spent_by_row):
	"""What a box answered: {row name: its spend counter for the user after, in nano-USD}."""
	from grove.billing.pricing import NANO

	for name, spent in spent_by_row.items():
		delta = frappe.db.get_value("Credit Discrepancy", name, "delta")
		frappe.db.set_value("Credit Discrepancy", name, {
			"correction": -delta, "resolution": "Gateway corrected", "gateway_spent": Decimal(spent) / NANO,
			"gateway_correction_pending": 0,
		})


def record(**facts):
	"""The one writer: one row per (Usage Record, pricing) the gateway charged differently."""
	facts = {k: float(v) if isinstance(v, Decimal) else v for k, v in facts.items()}
	return frappe.get_doc({"doctype": "Credit Discrepancy", **facts}).insert(ignore_permissions=True).name
