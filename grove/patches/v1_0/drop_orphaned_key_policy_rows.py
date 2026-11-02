"""Model Groups, Allow and Deny moved from Grove API Key to Grove Team (95795c4) and left the
keys' rows behind. `model_groups` is a key's field again, so those rows would load as grants."""

import frappe


def execute():
	for doctype in ("Model Group Row", "Grove Model Row"):
		frappe.db.delete(doctype, {"parenttype": "Grove API Key"})
