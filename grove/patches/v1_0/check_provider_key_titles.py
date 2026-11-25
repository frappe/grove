from grove.patches.v1_0.unique_provider_key_title import throw_on_twin_titles


def execute():
	"""Name twin key titles before the sync, which on a site of child-row keys indexes them while
	every provider is empty and leaves the fill patch a raw IntegrityError."""
	throw_on_twin_titles()
