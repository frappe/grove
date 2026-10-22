"""`billed` on Usage Record and `free` on Grove Team left the doctypes: every record is charged
and every team is. The columns go with them, once the records have merged and the sync has put
the unique on (drain id, key, day) in."""

import frappe


def execute():
	for doctype, column in (("Usage Record", "billed"), ("Grove Team", "free")):
		if frappe.db.has_column(doctype, column):
			frappe.db.sql_ddl(f"alter table `tab{doctype}` drop column `{column}`")
