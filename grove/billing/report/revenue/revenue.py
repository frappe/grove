"""Revenue per model, API key, team or day: what each billed drain was charged — Grove's price at
the pricing the gateway charged — plus the corrections of discrepancies Grove resolved, summed in
one grouped query over `money_rows`. A Free team's usage is not billed, so it is not revenue."""

import frappe

from grove.billing.doctype.usage_record.usage_record import money_rows

GROUP_BY = {
	"Model": ("model", "Data", None),
	"API Key": ("api_key", "Link", "Grove API Key"),
	"Team": ("team", "Link", "Central Team"),
	"Day": ("day", "Date", None),
}
FILTERS = ("model", "api_key", "team")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	group_by = filters.group_by or "Model"
	expr, fieldtype, options = GROUP_BY[group_by]
	columns = [
		{"fieldname": "label", "label": group_by, "fieldtype": fieldtype, "options": options, "width": 260},
		{"fieldname": "requests", "label": "Requests", "fieldtype": "Int", "width": 120},
		{"fieldname": "revenue", "label": "Revenue (USD)", "fieldtype": "Currency", "width": 140},
	]
	table, params = money_rows(
		{name: filters.get(name) or None for name in FILTERS}, from_date=filters.from_date, to_date=filters.to_date
	)
	rows = frappe.db.sql(
		f"""select {expr} as label, sum(requests) as requests, sum(cost) as revenue
		from {table} where billed = 1 group by {expr} order by revenue desc""",
		params,
		as_dict=True,
	)
	return columns, [{**row, "requests": int(row.requests or 0), "revenue": float(row.revenue or 0)} for row in rows]
