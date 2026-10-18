"""The gateway side of a correction is a Gateway Spend Adjustment of its own now, not three fields
and a tick on the Credit Discrepancy. Every row decided against the gateway gets one: Applied with
the box's answer where it had answered, Pending where it had not, and a pending row's decision is
stamped on the discrepancy as the button now stamps it. Then the three columns go."""

import frappe


def execute():
	if not frappe.db.has_column("Credit Discrepancy", "gateway_correction_pending"):
		return
	rows = frappe.db.sql(
		"""select name, team, api_key, gateway_store, delta, gateway_spent, resolved_on, modified,
		gateway_correction_pending as pending
		from `tabCredit Discrepancy`
		where (gateway_correction_pending = 1 or resolution = 'Gateway corrected')
		and name not in (select credit_discrepancy from `tabGateway Spend Adjustment`)""",
		as_dict=True,
	)
	for row in rows:
		# A patch keeps the creation it is handed; outside one, insert() would stamp now.
		frappe.get_doc(
			{
				"doctype": "Gateway Spend Adjustment",
				"credit_discrepancy": row.name,
				"team": row.team,
				"api_key": row.api_key,
				"gateway_store": row.gateway_store,
				"adjustment": -row.delta,
				"status": "Pending" if row.pending else "Applied",
				"gateway_spent": None if row.pending else row.gateway_spent,
				"applied_on": None if row.pending else row.resolved_on,
				"creation": row.modified if row.pending else row.resolved_on,
			}
		).insert()
		if row.pending:
			frappe.db.set_value(
				"Credit Discrepancy",
				row.name,
				{"resolution": "Gateway corrected", "resolved_on": row.modified},
				update_modified=False,
			)

	for column in ("gateway_correction_pending", "gateway_spent", "correction"):
		frappe.db.sql_ddl(f"alter table `tabCredit Discrepancy` drop column {column}")
