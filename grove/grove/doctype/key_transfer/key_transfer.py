# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class KeyTransfer(Document):
	"""One hand-over of a Grove API Key between teams, written by `GroveAPIKey.transfer`: `creation`
	is when and `owner` who. Never read to decide anything."""


def transfers_of(keys):
	"""{key: its hand-overs, oldest first}."""
	rows = frappe.get_list(
		"Key Transfer",
		filters={"api_key": ("in", keys)},
		fields=["api_key", "from_team", "to_team", "creation"],
		order_by="creation",
	)
	transfers = {}
	for row in rows:
		transfers.setdefault(row.pop("api_key"), []).append(row)
	return transfers
