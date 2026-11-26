# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ModelLimit(Document):
	"""One cap on its parent: `value` of `metric` per `window`, on every model or on the one linked,
	which holds in that model doc's geography (everywhere for a model of ours). The window resets on
	the UTC clock."""

	pass
