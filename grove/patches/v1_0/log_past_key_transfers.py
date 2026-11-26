import frappe


def execute():
	"""One Key Transfer per hand-over made before the doctype, off the Version each left: its
	teams, title before, time and user. A move that kept the title left no title entry, so the
	key's own stands in."""
	versions = frappe.get_all(
		"Version",
		filters={"ref_doctype": "Grove API Key", "data": ("like", '%"team"%')},
		fields=["docname", "data", "creation", "owner"],
		order_by="creation",
	)
	for version in versions:
		entries = frappe.parse_json(version.data).get("changed") or []
		changed = {field: (before, after) for field, before, after in entries}
		if "team" not in changed:
			continue
		from_team, to_team = changed["team"]
		if "title" in changed:
			from_title = changed["title"][0]
		else:
			from_title = frappe.db.get_value("Grove API Key", version.docname, "title")
		doc = frappe.get_doc(
			{
				"doctype": "Key Transfer",
				"api_key": version.docname,
				"from_team": from_team,
				"to_team": to_team,
				"from_title": from_title,
			}
		)
		doc.flags.ignore_links = True
		doc.insert()
		doc.db_set({"creation": version.creation, "owner": version.owner}, update_modified=False)
