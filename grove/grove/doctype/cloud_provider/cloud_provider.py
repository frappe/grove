# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.model.document import Document

from grove.cloud_provider.base import build_cloud_client
from grove.cloud_provider.runpod import RunPodClient


class CloudProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key_id: DF.Data | None
		api_key: DF.Password
		gpu_types: DF.JSON | None
		gpu_types_updated: DF.Datetime | None
		provider_type: DF.Literal["runpod", "aws"]
		resource_type: DF.Literal["Machine", "Pod"]
	# end: auto-generated types

	def validate(self):
		self.resource_type = "Pod" if self.provider_type == "runpod" else "Machine"

	@property
	def credentials(self):
		"""(access key id, secret): both for AWS, the secret alone for RunPod. Throws when unset."""
		secret = self.get_password("api_key", raise_exception=False)
		if not secret or (self.provider_type == "aws" and not self.access_key_id):
			frappe.throw(f"Cloud Provider {self.name} has no credentials set.")
		return self.access_key_id, secret

	def client(self, region=None):
		"""This account's API client: the `CloudClient` for `region`, or RunPod, which has none."""
		access_key_id, secret = self.credentials
		if self.provider_type == "runpod":
			return RunPodClient(secret)
		return build_cloud_client(self.provider_type, access_key_id, secret, region)

	@frappe.whitelist()
	def fetch_gpu_types(self):
		"""Refresh the cached GPU-type list in the background; the form's HTML field renders it on
		reload. RunPod only."""
		if self.provider_type != "runpod":
			frappe.throw("GPU type listing is only available for RunPod providers.")
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw("Set an API key on this Cloud Provider first.")
		frappe.enqueue(
			"grove.grove.doctype.cloud_provider.cloud_provider.update_gpu_types",
			queue="short",
			timeout=120,
			cloud_provider=self.name,
		)
		frappe.msgprint(
			"Fetching GPU types in the background — reload the page in a few seconds.", alert=True
		)


def update_gpu_types(cloud_provider):
	"""Job: pull the provider's GPU types and cache them as JSON. Secure-cloud-available first
	(Grove uses Secure), then largest VRAM: the order the picker shows."""
	cp = frappe.get_doc("Cloud Provider", cloud_provider)
	types = cp.client().list_gpu_types()
	types.sort(key=lambda g: (not g.get("secureCloud"), -(g.get("memoryInGb") or 0)))
	cp.db_set("gpu_types", json.dumps(types), update_modified=False)
	cp.db_set("gpu_types_updated", frappe.utils.now(), update_modified=False)
	frappe.db.commit()
