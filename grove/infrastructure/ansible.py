# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
import os

import frappe

from grove.infrastructure import ansible_runner
from grove.utils import ansible_project_dir, playbooks_root


class AnsibleHost:
	"""Mixin for a doc with a box behind it. Callers name a playbook, never a project path, a
	server type or a Machine."""

	@property
	def playbook_machine(self):
		if not self.machine:
			frappe.throw(f"{self.doctype} {self.name} has no Machine to run a playbook against.")
		return self.machine

	def run_playbook(self, playbook, project=None, extravars=None, **kwargs):
		"""`project` names another doctype's folder when the play is shared — exporters.yml belongs
		to Monitoring Agent but runs against Inference and Gateway Server boxes."""
		ansible = Ansible(project_root=ansible_project_dir(project or self.doctype))
		return ansible.run_playbook(
			playbook_name=playbook,
			server_type=self.doctype,
			server_name=self.name,
			machine_name=self.playbook_machine,
			extravars=extravars,
			**kwargs,
		)


class Ansible:
	"""Two usage styles:

	1. Direct (ad-hoc): Ansible(playbook="site.yml", server="10.0.0.1", user="root", variables={...}).run()
	2. Doctype-integrated: Ansible(project_root=...).run_playbook(playbook_name=..., server_type=..., ...)
	"""

	def __init__(self, playbook=None, project_root=None, server=None, user=None, port=None, variables=None):
		self.playbook = playbook
		self.server = server
		self.user = user or "root"
		self.port = port or 22
		self.variables = variables or {}

		if project_root:
			self.project_root = project_root
		else:
			self.project_root = playbooks_root()

	def run(self):
		"""Ad-hoc mode: direct server connection, no Frappe tracking."""
		if not self.playbook or not self.server:
			raise ValueError("Ad-hoc mode requires playbook= and server= arguments")

		import json
		import subprocess

		playbook_path = self._resolve_playbook_path(self.playbook)

		cmd = [
			"ansible-playbook",
			playbook_path,
			"-i",
			f"{self.server},",
			"-u",
			self.user,
			"-p",
			str(self.port),
		]
		if self.variables:
			cmd.extend(["-e", json.dumps(self.variables)])

		env = os.environ.copy()
		roles_path = os.path.join(self.project_root, "roles")
		if os.path.isdir(roles_path):
			env["ANSIBLE_ROLES_PATH"] = roles_path

		rc = subprocess.call(cmd, cwd=self.project_root, env=env)
		return rc

	def _resolve_playbook_path(self, playbook_name):
		candidates = [
			os.path.join(self.project_root, "playbooks", playbook_name),
			os.path.join(self.project_root, playbook_name),
			os.path.join(self.project_root, f"{playbook_name}.yml")
			if not playbook_name.endswith(".yml")
			else None,
		]

		for path in candidates:
			if path and os.path.isfile(path):
				return path

		raise FileNotFoundError(
			f"Playbook '{playbook_name}' not found in {self.project_root}. "
			f"Searched: playbooks/{playbook_name}, {playbook_name}, {playbook_name}.yml"
		)

	def run_playbook(
		self,
		playbook_name,
		server_type,
		server_name,
		machine_name,
		extravars=None,
		skip_tags=None,
		reference_doctype=None,
		reference_docname=None,
	):
		"""Doctype-integrated mode, tracked as Ansible Play/Task docs. `machine_name` is the Machine
		doc: same as `server_name` for Machine, different for role servers. Returns (play_name, rc),
		rc 0 = success."""
		playbook_path = self._resolve_playbook_path(playbook_name)

		kwargs = {
			"playbook": os.path.basename(playbook_path),
			"server_type": server_type,
			"server_name": server_name,
			"machine_name": machine_name,
			"project_dir": self.project_root,
			"extravars": extravars or {},
		}
		if skip_tags:
			kwargs["skip_tags"] = skip_tags
		if reference_doctype:
			kwargs["reference_doctype"] = reference_doctype
		if reference_docname:
			kwargs["reference_docname"] = reference_docname

		return ansible_runner.run_play(**kwargs)


_ansible_instance = None


def task_result(play_name, task_name):
	"""One task's result off its Ansible Task doc; a role's task is recorded as `<role> : <name>`.
	Empty when the task never ran — an unreachable host has no result to report."""
	output = frappe.db.get_value("Ansible Task", {"play": play_name, "task_name": task_name}, "output")
	try:
		return json.loads(output) if output else {}
	except json.JSONDecodeError:
		return {}


def get_ansible(project_root=None):
	global _ansible_instance
	if _ansible_instance is None:
		_ansible_instance = Ansible(project_root)
	return _ansible_instance
