"""A vendor key was a row of Model Provider's Keys table; it is a document of its own now and
names its provider itself. The name stays: the gateways count each key under it."""

import frappe


def execute():
	frappe.db.sql("update `tabModel Provider Key` set model_provider = parent where model_provider is null")
