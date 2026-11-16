"""Provider Key Stats named a key by provider and Title; it links the key now and the Title field is
gone. Each row from before the id was kept takes the id of the key so titled there, when that key
already existed as the row landed, and the column goes with its index."""

import frappe


def execute():
	if not frappe.db.has_column("Provider Key Stats", "key_title"):
		return
	frappe.db.sql(
		"""update `tabProvider Key Stats` s
		join `tabModel Provider Key` k
			on k.model_provider = s.model_provider and k.title = s.key_title and k.creation <= s.creation
		set s.key_id = k.name
		where coalesce(s.key_id, '') = ''"""
	)
	if frappe.db.has_index("tabProvider Key Stats", "key_title_creation_index"):
		frappe.db.sql_ddl("alter table `tabProvider Key Stats` drop index `key_title_creation_index`")
	frappe.db.sql_ddl("alter table `tabProvider Key Stats` drop column `key_title`")
