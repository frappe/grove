# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ModelLimit(Document):
	"""One cap on its parent: `value` of `metric` per `window`, on every model or the one linked."""

	pass
