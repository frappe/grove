# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class EngineImageProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		registry_host: DF.Data
		token: DF.Password | None
		username: DF.Data | None
	# end: auto-generated types

	"""A container registry (ghcr.io, docker.io, a private one) and the credentials to pull
	from it. Registry-agnostic: one record per registry, shared by its Engine Images."""

	pass
