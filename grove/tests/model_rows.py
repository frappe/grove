"""Model rows as `models_in` reads them, for the route tests that mock `frappe.get_all`. A fixture
names its docs by the key for readability; `model_key` is set from the name unless given."""


def model_row(name, provider_name="frappe", geography=None, **fields):
	self_hosted = provider_name == "frappe"
	return {
		"name": name,
		"model_key": fields.pop("model_key", name),
		"provider_name": provider_name,
		"is_self_hosted": self_hosted,
		"geography": None if self_hosted else (geography or "in"),
		"published": 1,
		**fields,
	}


def placement(model, **fields):
	"""A replica or pod row: links the doc and carries its fetched key."""
	return {"model": model, "model_key": model, **fields}
