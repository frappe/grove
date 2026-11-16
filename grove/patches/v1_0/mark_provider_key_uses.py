"""Model Provider Key says what it is used for now. Every key was dialled by the gateways, and
Baseten's also read its billing (the one vendor with a billing read), so each keeps its job."""

import frappe


def execute():
	frappe.db.sql(
		"update `tabModel Provider Key` set used_for = 'API Calls' where coalesce(used_for, '') = ''"
	)
	frappe.db.sql(
		"""update `tabModel Provider Key` k
		join `tabModel Provider` p on p.name = k.model_provider
		set k.used_for = 'API Calls and Billing'
		where p.provider_name = 'baseten' and k.used_for = 'API Calls'"""
	)
