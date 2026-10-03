"""Revenue per model, API key, user or day: what each billed drain was charged — Grove's price at
the pricing the gateway charged — summed in one grouped query over the records' per-model detail.
A Free user's usage is not billed, so it is not revenue."""

import frappe

from grove.grove.doctype.usage_record.usage_record import usage_table

GROUP_BY = {
	# The key the usage names, not a doc: one key may be a doc per geography.
	"Model": ("u.model", "Data", None),
	"API Key": ("r.api_key", "Link", "Grove API Key"),
	"Grove User": ("r.user", "Link", "Grove User"),
	"Day": ("r.day", "Date", None),
}
FILTERS = {"model": "u.model", "api_key": "r.api_key", "grove_user": "r.user"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	group_by = filters.group_by or "Model"
	expr, fieldtype, options = GROUP_BY[group_by]
	columns = [
		{"fieldname": "label", "label": group_by, "fieldtype": fieldtype, "options": options, "width": 260},
		{"fieldname": "requests", "label": "Requests", "fieldtype": "Int", "width": 120},
		{"fieldname": "revenue", "label": "Revenue (USD)", "fieldtype": "Currency", "width": 140},
	]
	conditions = " ".join(f"and {column} = %({name})s" for name, column in FILTERS.items() if filters.get(name))
	rows = frappe.db.sql(
		f"""select {expr} as label, sum(u.requests) as requests, sum(u.grove_cost) as revenue
		from `tabUsage Record` r, {usage_table()}
		where r.billed = 1 and r.day between %(from_date)s and %(to_date)s {conditions}
		group by {expr} order by revenue desc""",
		filters,
		as_dict=True,
	)
	return columns, [{**row, "requests": int(row.requests or 0), "revenue": float(row.revenue or 0)} for row in rows]
