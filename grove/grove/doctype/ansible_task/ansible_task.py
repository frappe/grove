# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
import frappe
from frappe.model.document import Document


class AnsibleTask(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		duration: DF.Float
		host: DF.Data | None
		output: DF.Code | None
		play: DF.Link | None
		status: DF.Literal["Pending", "Running", "Success", "Failure", "Unreachable", "Skipped"]
		task_name: DF.Data | None
	# end: auto-generated types

	def on_update(self):
		frappe.publish_realtime(
			"ansible_play_update",
			doctype="Ansible Play",
			docname=self.play,
			message={"id": self.play},
		)
