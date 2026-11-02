from decimal import Decimal

import frappe
from frappe.model.document import Document


class GatewaySpendAdjustment(Document):
	"""The gateway side of a Cost Discrepancy resolved as the gateway's error: what the box's
	spend counter for the key on that store is owed, sent by the projection tick until the box
	answers, then the counter it reported. Written by `CostDiscrepancy.adjust_gateway`, moved to
	Applied by `mark_corrected` (with no counter when the store no longer holds the key: revoked
	since, nothing left to lift), never edited by hand. The decision stays on the Cost Discrepancy,
	and Grove's own money never moves on this side."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		adjustment: DF.Currency
		api_key: DF.Link
		applied_on: DF.Datetime | None
		cost_discrepancy: DF.Link
		gateway_spent: DF.Currency
		gateway_store: DF.Link
		status: DF.Literal["Pending", "Applied"]
		team: DF.Link
	# end: auto-generated types


def pending_adjustments():
	"""{store: [spend-adjust body]} for every adjustment no gateway has applied, oldest first. The
	box applies each once under the discrepancy's name, so sending again is safe."""
	from grove.billing.pricing import nano

	# ponytail: the box forgets an id after 7 days, so a row pending longer could apply twice.
	# Keep ids forever in pathway if one ever does.
	rows = frappe.get_all(
		"Gateway Spend Adjustment",
		filters={"status": "Pending"},
		fields=["cost_discrepancy", "api_key", "gateway_store", "adjustment"],
		order_by="creation asc",
	)
	hashes = dict(
		frappe.get_all(
			"Grove API Key",
			{"name": ("in", [r.api_key for r in rows] or [""])},
			["name", "key_hash"],
			as_list=True,
		)
	)
	owed = {}
	for row in rows:
		body = {
			"key": hashes.get(row.api_key, ""),
			"delta": nano(row.adjustment),
			"id": row.cost_discrepancy,
		}
		owed.setdefault(row.gateway_store, []).append(body)
	return owed


def mark_corrected(spent_by_id):
	"""What a box answered: {discrepancy name: its spend counter for the key after, in nano-USD,
	or None for a key it no longer holds}. The adjustment under each is Applied."""
	from grove.billing.pricing import NANO

	for name, spent in spent_by_id.items():
		outcome = {"status": "Applied", "applied_on": frappe.utils.now_datetime()}
		if spent is not None:
			outcome["gateway_spent"] = Decimal(spent) / NANO
		frappe.db.set_value("Gateway Spend Adjustment", {"cost_discrepancy": name}, outcome)
