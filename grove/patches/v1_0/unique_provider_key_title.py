import frappe
from frappe import _
from frappe.query_builder.functions import Count

from grove.grove.doctype.model_provider_key.model_provider_key import on_doctype_update


def execute():
	"""Index a provider's key titles unique. A site that holds twins stops here until they are
	renamed by hand: a title is what the usage page shows, so it is never changed for anyone."""
	twins = get_twin_titles()
	if twins:
		lines = [
			_("{0} ({1}), title {2}: {3} keys").format(
				twin.provider_name, twin.model_provider, frappe.bold(twin.title), twin.key_count
			)
			for twin in twins
		]
		frappe.throw(
			_("Model Provider Keys of one provider share a title. Rename them, then migrate again:")
			+ "<br>"
			+ "<br>".join(lines),
			title=_("Duplicate Key Titles"),
		)
	on_doctype_update()


def get_twin_titles():
	"""Each provider and title more than one key carries, with how many carry it."""
	key = frappe.qb.DocType("Model Provider Key")
	provider = frappe.qb.DocType("Model Provider")
	keys = Count(key.name)
	return (
		frappe.qb.from_(key)
		.left_join(provider)
		.on(provider.name == key.model_provider)
		.select(provider.provider_name, key.model_provider, key.title, keys.as_("key_count"))
		.groupby(key.model_provider, provider.provider_name, key.title)
		.having(keys > 1)
		.run(as_dict=True)
	)
