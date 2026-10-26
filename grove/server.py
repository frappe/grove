import frappe

from grove.ansible import AnsibleHost
from grove.naming import GeneratedName, short_name
from grove.utils import validate_label_name


class Server(GeneratedName, AnsibleHost):
	def get_generated_name(self):
		return self.machine

	def autoname(self):
		super().autoname()
		validate_label_name(self.doctype, self.short_name)
		if self.short_name != self.name and self.name != f"{self.short_name}.{self.fleet_zone}":
			frappe.throw(
				f"{self.doctype} name '{self.name}' must be one label, or one label under its "
				f"Geography's zone ('{self.fleet_zone or 'none set'}')."
			)

	@property
	def short_name(self):
		return short_name(self.name)

	@property
	def fleet_zone(self):
		return frappe.db.get_value("Geography", self.geography, "fleet_zone")

	@property
	def archive_blockers(self):
		return []

	@frappe.whitelist()
	def archive(self):
		if blockers := self.archive_blockers:
			frappe.throw("<br>".join(blockers), title=f"{self.name} cannot be archived")

		machine = frappe.get_doc("Machine", self.machine) if self.machine else None
		if machine and machine.cloud_provider:
			machine.terminate()
		self.reload()
		if self.status != "Terminated":
			self.status = "Terminated"
			self.save()

		frappe.msgprint(f"Archiving {self.name}.", alert=True)
