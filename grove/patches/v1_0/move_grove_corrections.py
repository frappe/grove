"""A Grove correction is a Usage Correction of its own now, not a stamp on the Credit Discrepancy.
Every row Grove corrected gets one, on the day it counted and landed when it landed: `creation`
is the row's `resolved_on`, since Central's invoice window read the correction by that moment and
must not bill it again. Then `resolved_day` and its two indexes go, on a site that had them."""

import frappe
from frappe.utils import getdate


def execute():
	rows = frappe.db.sql(
		"""select d.name, d.team, d.api_key, d.pricing, d.correction, d.resolved_on, m.model_key
		from `tabCredit Discrepancy` d
		left join `tabModel Pricing` p on p.name = d.pricing left join `tabModel` m on m.name = p.model
		where d.resolution = 'Grove corrected'
		and d.name not in (select credit_discrepancy from `tabUsage Correction`)""",
		as_dict=True,
	)
	for row in rows:
		# A patch keeps the creation it is handed; outside one, insert() would stamp now.
		frappe.get_doc(
			{
				"doctype": "Usage Correction",
				"credit_discrepancy": row.name,
				"team": row.team,
				"api_key": row.api_key,
				"model": row.model_key or "",
				"pricing": row.pricing,
				"day": getdate(row.resolved_on),
				"cost": row.correction,
				"creation": row.resolved_on,
			}
		).insert()

	if not frappe.db.has_column("Credit Discrepancy", "resolved_day"):
		return
	for index in ("team_resolved_day_index", "api_key_resolved_day_index"):
		if frappe.db.has_index("tabCredit Discrepancy", index):
			frappe.db.sql_ddl(f"alter table `tabCredit Discrepancy` drop index {index}")
	frappe.db.sql_ddl("alter table `tabCredit Discrepancy` drop column resolved_day")
