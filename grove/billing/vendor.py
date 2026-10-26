from datetime import timedelta
from decimal import Decimal

import frappe
import requests
from frappe.utils import getdate

BASETEN_API = "https://api.baseten.co"
TIMEOUT = 30
RANGE_DAYS = 90


def vendor_figures(providers, from_date, to_date):
	"""{(day, upstream model id): {requests, input_tokens, cached_tokens, output_tokens, billed}} for
	every UTC day in [from_date, to_date] the vendor saw traffic under these Model Provider records'
	keys. The vendor bills per account, so the read is once per account: a key row's Account, or
	the key itself when it names none. Two keys of one account (a rotation) count once, two
	accounts in one record both count, and the same key in two geography records counts once. The
	model id is the vendor's own spelling, what `Model.upstream_model_id` holds."""
	vendor = providers[0].provider_name
	if vendor != "baseten":
		frappe.throw(f"No billing read for {vendor} yet: only baseten is reconciled.")
	if (to_date - from_date).days >= RANGE_DAYS:
		frappe.throw(f"{vendor} answers at most {RANGE_DAYS} days at a time.")

	secret_by_account = {}
	for provider in providers:
		for row in provider.api_keys:
			if not row.disabled:
				secret = row.get_password("api_key")
				secret_by_account.setdefault(row.account or secret, secret)
	if not secret_by_account:
		frappe.throw(f"{vendor} has no enabled key to read its billing with.")

	figures = {}
	for secret in secret_by_account.values():
		read_baseten(secret, from_date, to_date, figures)
	return figures


def read_baseten(secret, from_date, to_date, figures):
	"""Baseten's usage API (seven day-buckets a page, so 90 days is about 13 GETs) and its cost API
	(31 days a page), each grouped by model, summed into `figures` by (day, model). `subtotal` is a
	decimal string and is kept exact."""
	headers = {"Authorization": f"Bearer {secret}"}
	end = str(to_date + timedelta(days=1))

	def figure(day, model):
		blank = {
			"requests": 0,
			"input_tokens": 0,
			"cached_tokens": 0,
			"output_tokens": 0,
			"billed": Decimal(0),
		}
		return figures.setdefault((getdate(day), model), blank)

	usage = {
		"start_time": f"{from_date}T00:00:00Z",
		"end_time": f"{end}T00:00:00Z",
		"bucket_width": "1d",
		"group_by": "model",
	}
	for item in paged(f"{BASETEN_API}/v1/model_apis/usage", headers, usage):
		for row in item.get("results") or []:
			tokens = figure(item["start_time"][:10], row["model"])
			tokens["requests"] += row.get("request_count") or 0
			tokens["input_tokens"] += row.get("input_tokens") or 0
			tokens["cached_tokens"] += row.get("cached_input_tokens") or 0
			tokens["output_tokens"] += row.get("output_tokens") or 0

	cost = {"start_date": str(from_date), "end_date": end, "group_by": "model", "limit": 31}
	for item in paged(f"{BASETEN_API}/v1/billing/model_apis", headers, cost):
		for row in item.get("results") or []:
			figure(item["date"], row["model"])["billed"] += Decimal(row.get("subtotal") or "0")


def paged(url, headers, params):
	"""Every `items` entry across the pages: the first GET with `params`, the rest with the cursor
	alone, since Baseten ignores the other parameters once a cursor is passed."""
	while True:
		response = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
		if not response.ok:
			frappe.throw(f"baseten answered {response.status_code} on {url}: {response.text[:200]}")
		body = response.json()
		yield from body.get("items") or []
		pagination = body.get("pagination") or {}
		if not pagination.get("has_more"):
			return
		params = {"cursor": pagination["cursor"]}
