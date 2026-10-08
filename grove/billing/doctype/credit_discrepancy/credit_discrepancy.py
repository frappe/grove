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
		grove_value: DF.Currency
		note: DF.SmallText | None
		pricing: DF.Link | None
		resolution: DF.Literal['', 'Grove corrected', 'Gateway corrected']
		team: DF.Link | None
		usage_record: DF.Link | None
	# end: auto-generated types

	@frappe.whitelist()
	def correct_grove(self):
		"""Grove priced it wrong: the key's `spent` moves by the delta and the team is settled, so
		Grove's balance meets the gateway's."""
		from grove.billing.pricing import settle

		self.check_open()
		delta = Decimal(str(self.delta))
		frappe.db.sql("update `tabGrove API Key` set spent = spent + %s where name = %s", [delta, self.api_key])
		settle(self.team)
		self.db_set({"correction": delta, "resolution": "Grove corrected"})

	@frappe.whitelist()
	def correct_gateway(self):
		"""The gateway charged it wrong: its spend counter for the key on that store is owed minus
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
		fields=["name", "api_key", "gateway_store", "delta"], order_by="creation asc",
	)
	hashes = dict(
		frappe.get_all("Grove API Key", {"name": ("in", [r.api_key for r in rows] or [""])}, ["name", "key_hash"], as_list=True)
	)
	owed = {}
	for row in rows:
		# The box keys its records on the hash, not the doc name the usage is attributed under.
		body = {"key": hashes.get(row.api_key, ""), "delta": -nano(row.delta), "id": row.name}
		owed.setdefault(row.gateway_store, []).append(body)
	return owed


def mark_corrected(spent_by_row):
	"""What a box answered: {row name: its spend counter for the key after, in nano-USD}."""
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
