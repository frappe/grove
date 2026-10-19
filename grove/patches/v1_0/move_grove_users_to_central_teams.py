"""Grove User → Central Team, with the policy moved onto each key.

A user becomes a team named by its Central id (its old doc name when it had none). Every key of
the user gets the team, the user's geography, groups, allow/deny and limits; every key starts at a cap of 0 and is capped by hand. Records, stuck rows and discrepancies move to the team. Runs once: a site with no Grove User doctype left (moved by hand) is left alone."""

import frappe

from grove.access import DEFAULT_LIMITS

CHILDREN = ("Model Group Row", "Grove Model Row", "Model Limit")
SKIP = ("name", "parent", "parenttype", "creation", "modified", "owner", "modified_by", "docstatus", "idx")
RELINK = (
	("Usage Record", "user"), ("Stuck Usage", "grove_user"), ("Credit Discrepancy", "grove_user"),
)


def execute():
	if not frappe.db.exists("DocType", "Grove User") or not frappe.db.table_exists("Grove User"):
		return
	users = frappe.db.sql("select name, reference, email, log_payloads, geography from `tabGrove User`", as_dict=1)
	teams = {u.name: make_team(u) for u in users}
	for key in frappe.db.sql("select name, user, status from `tabGrove API Key` where user is not null", as_dict=1):
		move_key(key, next(u for u in users if u.name == key.user), teams[key.user])
	for doctype, field in RELINK:
		if frappe.db.has_column(doctype, field):
			for old, new in teams.items():
				frappe.db.sql(f"update `tab{doctype}` set team = %s where `{field}` = %s", [new, old])
	frappe.db.sql(
		"""update `tabCredit Discrepancy` d join `tabGrove API Key` k on k.name = d.api_key
		set d.team = k.team where ifnull(d.team, '') = ''"""
	)
	for child in CHILDREN:
		frappe.db.sql(f"delete from `tab{child}` where parenttype = 'Grove User'")
	frappe.delete_doc("DocType", "Grove User", force=True, ignore_permissions=True)


def make_team(user):
	name = user.reference or user.name
	if frappe.db.exists("Central Team", name):
		return name
	return frappe.get_doc({
		"doctype": "Central Team", "__newname": name, "log_payloads": user.log_payloads,
	}).insert(ignore_permissions=True).name


def move_key(key, user, team):
	"""The user's policy rows onto the key; a live key the user had no rows for starts as a fresh
	key would, in the geography's default group under the default limits."""
	geography = user.geography or frappe.db.get_value("Geography", {"is_default": 1})
	frappe.db.set_value("Grove API Key", key.name, {"team": team, "geography": geography}, update_modified=False)
	for child in CHILDREN:
		if frappe.db.exists(child, {"parenttype": "Grove API Key", "parent": key.name}):
			continue
		for row in frappe.get_all(child, filters={"parenttype": "Grove User", "parent": user.name}, fields=["*"], order_by="idx"):
			copy = {f: v for f, v in row.items() if f not in SKIP}
			frappe.get_doc({**copy, "doctype": child, "parent": key.name, "parenttype": "Grove API Key"}).insert(ignore_permissions=True)
	if key.status != "active":
		return
	if not frappe.db.exists("Model Group Row", {"parenttype": "Grove API Key", "parent": key.name}):
		if default := frappe.db.get_value("Model Group", {"is_default": 1, "geography": geography}):
			frappe.get_doc({
				"doctype": "Model Group Row", "parent": key.name, "parenttype": "Grove API Key",
				"parentfield": "model_groups", "model_group": default,
			}).insert(ignore_permissions=True)
	if not frappe.db.exists("Model Limit", {"parenttype": "Grove API Key", "parent": key.name}):
		for limit in DEFAULT_LIMITS:
			frappe.get_doc({
				"doctype": "Model Limit", "parent": key.name, "parenttype": "Grove API Key", "parentfield": "limits", **limit,
			}).insert(ignore_permissions=True)
