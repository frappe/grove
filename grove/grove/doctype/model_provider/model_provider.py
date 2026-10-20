# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import re

import frappe
import requests
from frappe.model.document import Document

from grove.utils import slugify

ANTHROPIC_VERSION = "2023-06-01"

PROVIDER_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


class ModelProvider(Document):
	"""Who serves a model: the one flagged Self Hosted for our own engines, a vendor for a
	third-party API.

	Provider Name is the namespace every Model under it is named in, set once: it is already inside
	every route key and every usage bucket a customer was billed against. A vendor has one record
	per Geography under the same name, so the model id is the same everywhere.

	Self Hosted names ours: a Model with no provider is named under it, and only its models can be
	deployed. Every other provider is a vendor: a published Model there routes straight to it and no
	engine is ever started. The URL fields are the dialect declaration — one per front the vendor
	runs (OpenAI-compatible, Anthropic-compatible), either or both."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from grove.grove.doctype.model_provider_key.model_provider_key import ModelProviderKey

		anthropic_base_url: DF.Data | None
		api_keys: DF.Table[ModelProviderKey]
		api_version: DF.Data | None
		base_url: DF.Data | None
		geography: DF.Link | None
		is_self_hosted: DF.Check
		key_selection: DF.Literal["Round Robin"]
		provider_name: DF.Data
	# end: auto-generated types

	def validate(self):
		if not PROVIDER_NAME.fullmatch(self.provider_name or ""):
			frappe.throw(
				f"Provider name {self.provider_name!r} must be lowercase letters, digits and single "
				"hyphens — it is the prefix of every model id this provider serves."
			)

		# mandatory_depends_on is client-side only; this is the gate an API insert hits.
		if not self.is_self_hosted and not self.geography:
			frappe.throw(
				f"{self.provider_name} needs a Geography — only gateways in it may route to this vendor."
			)

		if self.is_self_hosted:
			self.validate_self_hosted()
		self.validate_siblings()
		self.validate_key_titles()

	def validate_self_hosted(self):
		"""One provider is ours, and it dials nothing — a URL is what makes a vendor."""
		if self.base_url or self.anthropic_base_url:
			frappe.throw(f"{self.provider_name} is Self Hosted, so there is no vendor URL to dial.")
		other = frappe.db.get_value(
			"Model Provider", {"is_self_hosted": 1, "name": ("!=", self.name)}, "provider_name"
		)
		if other:
			frappe.throw(f"{other} is already the Self Hosted provider — there is one.")

	def validate_siblings(self):
		"""Records sharing a name are one provider: one per Geography, all ours or all a vendor's.
		The unique index backs the first rule; this is the message an operator can act on."""
		siblings = frappe.get_all(
			"Model Provider",
			filters={"provider_name": self.provider_name, "name": ("!=", self.name)},
			fields=["geography", "is_self_hosted"],
		)
		for sibling in siblings:
			if bool(sibling.is_self_hosted) != bool(self.is_self_hosted):
				frappe.throw(
					f"{self.provider_name} is already named by a provider that is not the same kind."
				)
			if sibling.geography == self.geography:
				frappe.throw(f"{self.provider_name} already has a record in {self.geography}.")

	def validate_key_titles(self):
		"""A key is known by its title — Provider Key Stats names it so — and a blank one is the
		mandatory check's to refuse."""
		titles = [row.title for row in self.api_keys if row.title]
		repeated = sorted(title for title in set(titles) if titles.count(title) > 1)
		if repeated:
			frappe.throw(f"{self.provider_name} has two keys titled {repeated[0]!r}.")

	def on_update(self):
		if self.has_value_changed("is_self_hosted"):
			frappe.db.set_value(
				"Model", {"provider": self.name}, "provider_is_self_hosted", self.is_self_hosted
			)

	@frappe.whitelist()
	def fetch_models(self):
		"""Button: what the vendor serves, read off its `/v1/models`, split into the ids this record
		already holds and the ones it could add. One front is asked — the fronts are dialects of one
		vendor and list the same models, and an Anthropic shim beside an OpenAI front (DeepSeek's)
		has no list at all."""
		frappe.only_for("System Manager")
		enabled = [row for row in self.api_keys if not row.disabled]
		secret = enabled[0].get_password("api_key") if enabled else None
		if not ((self.base_url or self.anthropic_base_url) and secret):
			frappe.throw(f"{self.provider_name} has no front and key to ask for its models.")
		upstream = set(self.list_upstream_models(secret))
		rows = frappe.get_all("Model", {"provider": self.name}, ["model_id", "upstream_model_id"])
		held = {row.upstream_model_id or row.model_id for row in rows}
		return {"new": sorted(upstream - held), "held": sorted(upstream & held)}

	def list_upstream_models(self, secret):
		"""`GET <front>/v1/models` off the OpenAI front, else the Anthropic one — every page: Anthropic
		answers in pages of up to 1000 continued with `after_id`; OpenAI's is one page."""
		if self.base_url:
			url = f"{self.base_url.rstrip('/')}/v1/models"
			headers = {"Authorization": f"Bearer {secret}"}
			params = {}
		else:
			url = f"{self.anthropic_base_url.rstrip('/')}/v1/models"
			headers = {"x-api-key": secret, "anthropic-version": self.api_version or ANTHROPIC_VERSION}
			params = {"limit": 1000}
		ids = []
		while True:
			response = requests.get(url, headers=headers, params=params, timeout=30)
			if not response.ok:
				frappe.throw(
					f"{self.provider_name} answered {response.status_code} on {url}: {response.text[:200]}"
				)
			body = response.json()
			ids += [model["id"] for model in body["data"]]
			if not body.get("has_more"):
				return ids
			params["after_id"] = body["last_id"]

	@frappe.whitelist()
	def add_models(self, model_ids: dict[str, str]):
		"""Button: {upstream id: our id} as unpublished Models under this record. Our id is
		slugified; the vendor's own spelling rides `upstream_model_id` when it differs."""
		frappe.only_for("System Manager")
		keys = []
		for upstream_id, model_id in model_ids.items():
			model_id = slugify(model_id.replace("/", "-"))
			model = {
				"doctype": "Model",
				"provider": self.name,
				"model_id": model_id,
				"upstream_model_id": upstream_id if upstream_id != model_id else None,
			}
			keys.append(frappe.get_doc(model).insert().model_key)
		return keys


def on_doctype_update():
	"""One record per provider per geography, which is what lets sync key a geography's vendors
	by name."""
	frappe.db.add_unique(
		"Model Provider", ["provider_name", "geography"], constraint_name="unique_provider_geography"
	)


def self_hosted_provider():
	"""The one provider our own engines serve under, None until one is flagged."""
	return frappe.db.get_value("Model Provider", {"is_self_hosted": 1}, "name")
