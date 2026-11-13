"""A Grove correction is a Usage Record naming its Cost Discrepancy now, not a Usage Correction.
Each row moves under its own name, day and `creation`: Central's invoice window read it by that
moment and must not bill it again. The old table stays, unread."""

import frappe


def execute():
	if not frappe.db.table_exists("Usage Correction"):
		return
	frappe.db.sql(
		"""insert into `tabUsage Record`
		(name, creation, modified, owner, modified_by, api_key, team, day, cost, cost_discrepancy, `usage`)
		select c.name, c.creation, c.modified, c.owner, c.modified_by, c.api_key, c.team, c.day, c.cost,
		c.cost_discrepancy, json_array(json_object(
			'model', coalesce(c.model, ''), 'pricing', c.pricing, 'requests', 0, 'grove_cost', c.cost))
		from `tabUsage Correction` c
		where c.cost_discrepancy not in
		(select cost_discrepancy from `tabUsage Record` where cost_discrepancy is not null)"""
	)
