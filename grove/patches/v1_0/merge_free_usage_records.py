"""Every Usage Record is charged now, so a key's day in a drain is one record. A drain that
spanned a Free flip left a free row beside the charged one; it is folded into the charged row,
cost and all, before the unique on (drain id, key, day) goes in at the doctype sync. Runs before
the sync, while `billed` still exists."""

import frappe


def execute():
	if not frappe.db.has_column("Usage Record", "billed"):
		return
	pairs = frappe.db.sql(
		"""select drain_id, api_key, day from `tabUsage Record`
		group by drain_id, api_key, day having count(*) > 1""",
		as_dict=True,
	)
	for pair in pairs:
		rows = frappe.db.sql(
			"""select name, request_count, total_tokens, cost, gateway_cost, usage from `tabUsage Record`
			where drain_id = %(drain_id)s and api_key = %(api_key)s and day = %(day)s
			order by billed desc, creation""",
			pair,
			as_dict=True,
		)
		frappe.db.set_value("Usage Record", rows[0].name, merged(rows), update_modified=False)
		frappe.db.delete("Usage Record", {"name": ("in", [row.name for row in rows[1:]])})


def merged(rows):
	"""The one record `rows` make: the totals summed, the per-model entries of every row kept."""
	entries = [entry for row in rows for entry in frappe.parse_json(row.usage or "[]")]
	return {
		"request_count": sum(row.request_count or 0 for row in rows),
		"total_tokens": sum(row.total_tokens or 0 for row in rows),
		"cost": sum(row.cost or 0 for row in rows),
		"gateway_cost": sum(row.gateway_cost or 0 for row in rows),
		"usage": frappe.as_json(entries, indent=None),
	}
