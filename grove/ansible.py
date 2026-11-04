# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import os

import frappe

from grove import ansible_runner
from grove.utils import ansible_project_dir


class AnsibleHost:
	"""Mixin for a doc with a box behind it. Callers name a playbook, never a project path, a
	server type or a Machine."""

	@property
	def playbook_machine(self):
		if not self.machine:
			frappe.throw(f"{self.doctype} {self.name} has no Machine to run a playbook against.")
		return self.machine

	def run_playbook(self, playbook, project=None, extravars=None, **kwargs):
		"""Run one playbook against this doc's box, tracked as an Ansible Play. `project` names
		another doctype's folder when the play is shared: exporters.yml belongs to Monitoring
		Agent but runs against Inference and Gateway Server boxes."""
		project_dir = ansible_project_dir(project or self.doctype)
		if not os.path.isfile(os.path.join(project_dir, playbook)):
			raise FileNotFoundError(f"{playbook} is not in {project_dir}")
		return ansible_runner.run_play(
			playbook=playbook,
			server_type=self.doctype,
			server_name=self.name,
			machine_name=self.playbook_machine,
			project_dir=project_dir,
			extravars=extravars,
			**kwargs,
		)
