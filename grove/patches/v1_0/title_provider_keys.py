"""A key's Title is mandatory now: one saved without gets its row id, the name the gateway counts
it under."""

import frappe


def execute():
	frappe.db.sql("update `tabModel Provider Key` set title = name where coalesce(title, '') = ''")
