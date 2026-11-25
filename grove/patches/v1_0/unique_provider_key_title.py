import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, Count

from grove.grove.doctype.model_provider_key.model_provider_key import on_doctype_update


def execute():
	"""Index a provider's key titles unique. A site that holds twins stops here until they are
	renamed by hand: a title is what the usage page shows, so it is never changed for anyone."""
	throw_on_twin_titles()
	on_doctype_update()


def throw_on_twin_titles():
	"""Stop the migrate with each provider and title more than one key carries."""
	twins = get_twin_titles()
	if not twins:
		return
	lines = [
		_("{0} ({1}), title {2}: {3} keys").format(
			twin.provider_name, twin.provider, twin.title, twin.key_count
		)
		for twin in twins
	]
	frappe.throw(
		_("Model Provider Keys of one provider share a title. Rename them, then migrate again:")
		+ "\n"
		+ "\n".join(lines),
		title=_("Duplicate Key Titles"),
	)


def get_twin_titles():
	"""Each provider and title more than one key carries, with how many carry it."""
	key = frappe.qb.DocType("Model Provider Key")
	key_provider = get_key_provider_column(key)
	if key_provider is None:
		return []
	provider = frappe.qb.DocType("Model Provider")
	key_count = Count(key.name)
	return (
		frappe.qb.from_(key)
		.left_join(provider)
		.on(provider.name == key_provider)
		.select(provider.provider_name, key_provider.as_("provider"), key.title, key_count.as_("key_count"))
		.where(Coalesce(key.title, "") != "")
		.groupby(key_provider, provider.provider_name, key.title)
		.having(key_count > 1)
		.run(as_dict=True)
	)


def get_key_provider_column(key):
	"""Where a key names its provider: `model_provider`, else `parent` while keys were child rows.
	None on a site whose table or columns are not there yet."""
	doctype = "Model Provider Key"
	if not frappe.db.table_exists(doctype, cached=False) or not frappe.db.has_column(doctype, "title"):
		return None
	columns = [
		key[column] for column in ("model_provider", "parent") if frappe.db.has_column(doctype, column)
	]
	return Coalesce(*columns) if columns else None
