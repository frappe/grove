import frappe


def execute():
	"""`Central Team` → `Grove Team`: a team at Grove is any control client's, Central's or Press's.
	The table, every Link to it and the doc perms follow the rename; the model sync after this patch
	reads the moved json. In a patch Frappe does not touch the module folder, which git moved."""
	if frappe.db.exists("DocType", "Central Team") and not frappe.db.exists("DocType", "Grove Team"):
		frappe.rename_doc("DocType", "Central Team", "Grove Team", force=True)
