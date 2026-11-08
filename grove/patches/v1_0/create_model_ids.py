"""`Model.model_id` links a Model ID now: one record per id the site's Models already carry."""

import frappe

from grove.grove.doctype.model_id.model_id import ensure_model_id


def execute():
	for model_id in frappe.get_all("Model", pluck="model_id", distinct=True):
		ensure_model_id(model_id)
