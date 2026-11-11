"""The key title unique becomes (title, team): Frappe names a twin by the index's first column,
so a second key of one title reads "Title must be unique", not "Team must be unique".
The new index goes in before the old one goes, so no mint runs unguarded."""

import frappe

from grove.grove.doctype.grove_api_key.grove_api_key import on_doctype_update


def execute():
	on_doctype_update()
	if frappe.db.has_index("tabGrove API Key", "unique_team_title"):
		frappe.db.sql_ddl("alter table `tabGrove API Key` drop index unique_team_title")
