"""What Grove charged users for a vendor's models against what the vendor billed Grove, per model or
per UTC day and model: requests and tokens from both sides, what Grove charged, the vendor's
subtotal and the margin. The vendor is read live (`billing/vendor.py`); the Grove side is one
grouped query over the records' per-model detail. Yesterday is the latest day: today's figures still
move on both sides. A prepared report: it runs in the background and the latest run is shown."""

import frappe
from frappe.utils import getdate

from grove.billing.doctype.usage_record.usage_record import usage_table
from grove.billing.pricing import CounterTable
from grove.billing.vendor import vendor_figures
from grove.utils import utc_today

COLUMNS = [
	("day", "Day", "Date", 100),
	("model", "Model", "Data", 240),
	("requests_grove", "Requests (Grove)", "Int", 120),
	("requests_vendor", "Requests (Vendor)", "Int", 120),
	("input_grove", "Input (Grove)", "Int", 120),
	("input_vendor", "Input (Vendor)", "Int", 120),
	("cached_grove", "Cached (Grove)", "Int", 120),
	("cached_vendor", "Cached (Vendor)", "Int", 120),
	("output_grove", "Output (Grove)", "Int", 120),
	("output_vendor", "Output (Vendor)", "Int", 120),
	("charged", "Charged users (USD)", "Currency", 140),
	("vendor_billed", "Vendor billed (USD)", "Currency", 140),
	("margin", "Margin (USD)", "Currency", 120),
]
# Grove's counter behind each vendor figure; a derived variant of it (long prompts) is summed in.
TOKENS = {"input": "prompt_tokens", "cached": "cached_tokens", "output": "completion_tokens"}
FIGURES = ("requests", "input_tokens", "cached_tokens", "output_tokens")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from_date, to_date = getdate(filters.from_date), getdate(filters.to_date)
	if to_date >= utc_today():
		frappe.throw("Set To to yesterday or earlier: today's figures still move on both sides.")
	if from_date > to_date:
		frappe.throw("From is after To.")
	by_day = (filters.group_by or "Model") == "Day and Model"

	providers = vendor_records(filters.provider)
	keys = model_keys([provider.name for provider in providers])
	grove = grove_rows([key for names in keys.values() for key in names], from_date, to_date)
	vendor = vendor_figures(providers, from_date, to_date)

	columns = [
		{"fieldname": name, "label": label, "fieldtype": fieldtype, "width": width}
		for name, label, fieldtype, width in COLUMNS
		if by_day or name != "day"
	]
	return columns, joined(keys, grove, vendor, by_day)


def vendor_records(provider_name):
	"""The vendor's Model Provider docs, one per Geography. The filter names the vendor, not a
	record, since one account usually serves every geography."""
	names = frappe.get_all("Model Provider", filters={"provider_name": provider_name}, pluck="name")
	if not names:
		frappe.throw(f"No Model Provider is named {provider_name}.")
	return [frappe.get_doc("Model Provider", name) for name in names]


def model_keys(providers):
	"""{upstream id: [model keys]} of the records' Models: what the vendor names a model against what
	the usage names it. Two keys may share one upstream id."""
	rows = frappe.get_all(
		"Model",
		filters={"provider": ("in", providers)},
		fields=["model_key", "model_id", "upstream_model_id"],
	)
	keys = {}
	for row in rows:
		keys.setdefault(row.upstream_model_id or row.model_id, []).append(row.model_key)
	return keys


def grove_rows(model_keys, from_date, to_date):
	if not model_keys:
		return {}
	counters = CounterTable.load()
	sums = ", ".join(
		"sum("
		+ " + ".join(f"u.{name}" for name in [root, *derived_of(counters, root)])
		+ f") as {figure}_tokens"
		for figure, root in TOKENS.items()
	)
	rows = frappe.db.sql(
		f"""select r.day, u.model, sum(u.requests) as requests, {sums},
		sum(u.grove_cost) as charged
		from `tabUsage Record` r, {usage_table()}
		where r.day between %(from_date)s and %(to_date)s and u.model in %(models)s
		group by r.day, u.model""",
		{"from_date": from_date, "to_date": to_date, "models": model_keys},
		as_dict=True,
	)
	return {(row.day, row.model): row for row in rows}


def derived_of(counters, root):
	return [row.name for row in counters.rows if row.base_counter == root]


def joined(keys, grove, vendor, by_day):
	"""Both sides summed into one row per (day, upstream id), or per upstream id over the whole range,
	sorted. A model only the vendor saw is labelled by its upstream id; a key Grove holds by its
	model key."""
	upstream_of = {key: upstream for upstream, names in keys.items() for key in names}
	rows = {}

	def row(day, upstream):
		label = ", ".join(sorted(keys.get(upstream) or [])) or upstream
		fields = {"day": day, "model": label} if by_day else {"model": label}
		return rows.setdefault((day, upstream) if by_day else (upstream,), fields)

	for (day, key), g in grove.items():
		r = row(day, upstream_of[key])
		for figure in FIGURES:
			add(r, figure.removesuffix("_tokens") + "_grove", int(g[figure] or 0))
		add(r, "charged", float(g.charged or 0))
	for (day, upstream), v in vendor.items():
		r = row(day, upstream)
		for figure in FIGURES:
			add(r, figure.removesuffix("_tokens") + "_vendor", v[figure])
		add(r, "vendor_billed", float(v["billed"]))
	for r in rows.values():
		r["margin"] = round((r.get("charged") or 0) - (r.get("vendor_billed") or 0), 9)
	return [rows[key] for key in sorted(rows)]


def add(row, name, amount):
	row[name] = round((row.get(name) or 0) + amount, 9)
