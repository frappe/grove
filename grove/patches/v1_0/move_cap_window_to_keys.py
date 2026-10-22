"""The cap window moved from the team onto each key (`Grove API Key.cap_window`): a team mixes
lifetime-capped keys with monthly ones. Every key of a Total team is Total, so its gateway record
keeps `budget_window total` and its count; the team column then goes."""

import frappe


def execute():
	if not frappe.db.has_column("Grove Team", "key_cap_window"):
		return
	frappe.db.sql(
		"""update `tabGrove API Key` k join `tabGrove Team` t on t.name = k.team
		set k.cap_window = 'Total' where t.key_cap_window = 'Total'"""
	)
	frappe.db.sql_ddl("alter table `tabGrove Team` drop column if exists `key_cap_window`")
