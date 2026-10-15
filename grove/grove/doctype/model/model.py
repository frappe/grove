# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import requests

import frappe
from frappe.model.document import Document

from grove import failure
from grove.catalog import seed
from grove.grove.doctype.model_provider.model_provider import self_hosted_provider
from grove.utils import slugify

HF_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"
# Root listing with a size per file. The limit is well past any real shard count.
HF_TREE_URL = "https://huggingface.co/api/models/{repo}/tree/main?limit=1000"

# The two lists of a Model, and the Modality flag a word needs to go in each.
MODALITY_FIELDS = {"input_modalities": "is_input", "output_modalities": "is_output"}


class Model(Document):
	"""One model as one provider record serves it. The doc name is a hash; `model_key`
	(`<provider name>/<model id>`) is what callers send, the route key and the usage bucket — set
	on insert and frozen. A vendor with a record per Geography has a doc per record, all under one
	key, each with its own upstream id and pricing; a Link means the doc, the key means the id."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from grove.grove.doctype.model_modality_row.model_modality_row import ModelModalityRow

		attention_heads: DF.Int
		enable_auto_tool_choice: DF.Check
		enable_prefix_caching: DF.Check
		geography: DF.Link | None
		hf_repo: DF.Data | None
		hidden_layers: DF.Int
		input_modalities: DF.TableMultiSelect[ModelModalityRow]
		model_id: DF.Data
		model_key: DF.Data | None
		output_modalities: DF.TableMultiSelect[ModelModalityRow]
		provider: DF.Link | None
		provider_is_self_hosted: DF.Check
		published: DF.Check
		reasoning_parser: DF.Data | None
		thinking: DF.Check
		tool_call_parser: DF.Data | None
		torch_dtype: DF.Data | None
		upstream_model_id: DF.Data | None
		weights_gb: DF.Float
		weights_s3_uri: DF.Data | None
	# end: auto-generated types

	def validate(self):
		self.set_model_key()
		self.set_modalities()
		self.provider_is_self_hosted = self.is_self_hosted
		# mandatory_depends_on is client-side only; this is the gate an API insert hits.
		if self.is_self_hosted and not self.hf_repo:
			frappe.throw(
				f"{self.model_id} needs an HF Repo: nothing else says where its weights come from, "
				"and its provider serves nothing of its own.",
				frappe.MandatoryError,
			)

		self.validate_weights_source()
		if self.published and self.has_value_changed("published"):
			self.validate_publishable()

	def onload(self):
		self.set_onload("has_catalog_pricing", self.has_catalog_pricing)
		if self.published:
			self.set_onload("is_granted", self.is_granted)

	@property
	def has_catalog_pricing(self):
		"""The catalog prices this model and no Model Pricing names it yet."""
		return bool(seed.get_rates(self.model_key)) and not frappe.db.exists("Model Pricing", {"model": self.name})

	@property
	def is_granted(self):
		"""Someone can call it: a key's own Allow names it, or a Model Group with a key in it does.
		A group nobody is in grants nothing yet, the default one included."""
		rows = frappe.get_all(
			"Grove Model Row",
			filters={"model_key": self.model_key, "parentfield": ("in", ("allow", "models"))},
			fields=["parenttype", "parent"],
		)
		if any(row.parenttype == "Grove API Key" for row in rows):
			return True
		groups = [row.parent for row in rows if row.parenttype == "Model Group"]
		return bool(groups) and bool(
			frappe.db.exists("Model Group Row", {"parenttype": "Grove API Key", "model_group": ("in", groups)})
		)

	@frappe.whitelist()
	def load_pricing(self):
		"""Button: the catalog's rates as a Disabled draft. Enabling it stays the operator's call."""
		rates = seed.get_rates(self.model_key)
		if not rates:
			frappe.throw(f"The catalog holds no pricing for {self.model_key}.")
		if frappe.db.exists("Model Pricing", {"model": self.name}):
			frappe.msgprint(f"{self.model_key} already has a Model Pricing. Nothing was loaded.")
			return None
		pricing = {"doctype": "Model Pricing", "model": self.name, "status": "Disabled", "rates": rates}
		return frappe.get_doc(pricing).insert().name

	def validate_publishable(self):
		"""Publishing is the operator's call, and only a priced, served model can take it. Not an
		access gate — access is granted per key via Model Group or its own Allow."""
		if not frappe.db.exists("Model Pricing", {"model": self.name, "status": "Enabled"}):
			frappe.throw(
				f"{self.model_key} has no Enabled Model Pricing. Enable one first — zero rates serve it free."
			)
		if not is_reachable(self.name, provider=self.provider):
			frappe.throw(
				f"{self.model_key} has nothing serving it: no Active replica, Running pod or vendor endpoint."
			)

	def set_model_key(self):
		"""`<provider name>/<model id>`, normalised once and then frozen — the key is inside every
		route, grant and usage bucket, so an edit would rename a live model out from under its
		callers. The provider link may still move between records of the same name."""
		self.model_id = slugify(self.model_id)
		if not self.model_id:
			frappe.throw("No Model ID set")
		# slugify keeps a slash, and the slash separates provider from id — one here would name
		# `<provider>/a/b` and read as a provider nobody registered.
		if "/" in self.model_id:
			frappe.throw("Model ID cannot contain '/'")
		self.provider = self.provider or self_hosted_provider()
		if not self.provider:
			frappe.throw(
				"No Model Provider is marked Self Hosted, so a blank provider has nothing to default to."
			)
		expected = f"{self.provider_name}/{self.model_id}"
		if self.model_key and self.model_key != expected:
			frappe.throw(
				f"{self.model_key} is keyed under its provider: Provider can only move to another "
				"record of the same name.",
				frappe.CannotChangeConstantError,
			)
		self.model_key = expected

	def set_modalities(self):
		"""What the model takes and what it gives. A blank list is text — a chat model — and a
		word goes only in the list its Modality is for."""
		for fieldname, flag in MODALITY_FIELDS.items():
			if not self.get(fieldname):
				self.append(fieldname, {"modality": "Text"})
			words = [row.modality for row in self.get(fieldname)]
			known = frappe.get_all("Modality", filters={flag: 1}, pluck="name")
			wrong = sorted({word for word in words if word not in known or words.count(word) > 1})
			if wrong:
				frappe.throw(
					f"{self.meta.get_label(fieldname)} takes each of {', '.join(sorted(known))} once; "
					f"not {', '.join(wrong)}."
				)

	def validate_weights_source(self):
		"""The streamer reads safetensors out of a bucket; a GGUF ref names one file it cannot
		stream."""
		if not self.weights_s3_uri:
			return
		if not self.weights_s3_uri.startswith("s3://"):
			frappe.throw("Weights S3 URI must start with s3://")
		if self.gguf_quant:
			frappe.throw(
				"A GGUF ref cannot stream — the runai streamer needs safetensors. Clear "
				"Weights S3 URI, or point HF Repo at the safetensors repo."
			)

	@property
	def provider_name(self):
		"""The provider's name, read off the linked record: it prefixes this model's key."""
		return frappe.db.get_value("Model Provider", self.provider, "provider_name")

	@property
	def repo_id(self):
		"""The repo alone. A GGUF repo publishes a dozen quantizations, so vLLM is pointed at one
		with `unsloth/Qwen3-0.6B-GGUF:Q4_K_M` — a ref the HF API does not take."""
		return (self.hf_repo or "").split(":")[0]

	@property
	def gguf_quant(self):
		"""The quantization named after the colon, blank for a safetensors repo."""
		return (self.hf_repo or "").partition(":")[2]

	@property
	def is_self_hosted(self):
		"""Our own engines serve it — the provider is the flagged one. Read off the provider rather
		than the mirror, so the two cannot disagree."""
		return bool(
			self.provider and frappe.db.get_value("Model Provider", self.provider, "is_self_hosted")
		)

	def reject_if_vendor_served(self, what):
		"""Refuse a self-hosting operation on a model we do not host. The form hides these, but a
		whitelisted method is reachable without the button — and the errors underneath name a
		missing repo, which is true and no help at all."""
		if not self.is_self_hosted:
			frappe.throw(f"{self.model_key} is served by {self.provider_name}. {what}, and there is none.")

	@frappe.whitelist()
	def fetch_architecture(self):
		"""Button: read the shape off the repo's config.json, so the parallelism checks have real
		numbers instead of hand-typed ones."""
		self.reject_if_vendor_served("Architecture is read off an HF repo")
		if not self.hf_repo:
			frappe.throw("Set the HF Repo first — that's what's read.")
		config = self.get_hf_config()
		# Multimodal repos nest the language model's shape; the top level describes the whole
		# thing, vision tower included.
		shape = config.get("text_config") or config.get("llm_config") or {}
		heads = shape.get("num_attention_heads") or config.get("num_attention_heads")
		layers = shape.get("num_hidden_layers") or config.get("num_hidden_layers")
		if not (heads and layers):
			frappe.throw(f"{self.hf_repo}'s config.json has no head/layer count to read.")
		values = {"attention_heads": heads, "hidden_layers": layers}
		if dtype := config_dtype(config, shape):
			values["torch_dtype"] = dtype
		if weights_gb := self.get_weights_gb():
			values["weights_gb"] = weights_gb
		self.db_set(values)
		frappe.msgprint(
			f"{self.hf_repo}: {heads} attention heads, {layers} layers"
			+ (f", {values['weights_gb']} GB of weights." if "weights_gb" in values else ".")
		)
		return values

	@frappe.whitelist()
	def mirror_weights(self):
		"""Button: copy the safetensors off a box that serves this model into the weights bucket,
		then set Weights S3 URI so the next deploy streams them."""
		self.reject_if_vendor_served("Weights are mirrored off a box that serves the model")
		settings = frappe.get_single("Grove Settings")
		if not settings.weights_s3_write_environment:
			frappe.throw("Set Weights Bucket and the Mirror keys in Grove Settings first.")
		if self.gguf_quant:
			frappe.throw("A GGUF ref cannot be mirrored — the streamer needs safetensors.")
		if not mirror_server(self.name):
			frappe.throw(
				f"No Active Model Replica serves {self.model_key}. The mirror runs from a box "
				"that has the weights cached — deploy the model once first."
			)
		frappe.enqueue(
			"grove.grove.doctype.model.model.mirror_weights_to_s3",
			model=self.name,
			queue="long",
			timeout=28800,
		)
		frappe.msgprint(
			f"Mirroring {self.hf_repo} to {settings.weights_bucket} — watch the box's Ansible Plays.",
			alert=True,
		)

	def get_hf_config(self):
		"""The repo's config.json — its architecture."""
		return self._hf_json(HF_CONFIG_URL)

	def get_weights_gb(self):
		"""Size of the weights in GB — decimal, to match how GPU VRAM is quoted. The top-level
		safetensors shards as they are on disk, or the single GGUF file.

		Measured, not costed out from parameter counts: a packed quantization reports its
		CONTAINER type, so GLM-5.2-AWQ-INT4 comes back as 726 billion I32 and prices at 2959 GB
		against a real 474. Subfolders are skipped because that is where a repo keeps other
		quantizations of the same weights."""
		suffix = f"{self.gguf_quant}.gguf" if self.gguf_quant else ".safetensors"
		total_bytes = sum(
			entry.get("size") or 0
			for entry in self._hf_json(HF_TREE_URL)
			if entry.get("type") == "file" and is_root_weights_file(entry.get("path", ""), suffix)
		)
		return round(total_bytes / 1_000_000_000, 2) or None

	def _hf_json(self, url_template):
		"""Sends the site's HF token when there is one: gated repos 401 without it, as do repos
		that don't exist — HF doesn't distinguish."""
		token = frappe.conf.get("hf_token")
		headers = {"Authorization": f"Bearer {token}"} if token else {}
		response = requests.get(url_template.format(repo=self.repo_id), headers=headers, timeout=30)
		if response.status_code in (401, 403, 404):
			frappe.throw(
				f"Hugging Face returned {response.status_code} for {self.hf_repo} — the repo is "
				"gated, private or misspelled. Gated repos need hf_token in the site config."
			)
		if not response.ok:
			frappe.throw(f"Hugging Face returned {response.status_code} for {self.hf_repo}.")
		return response.json()


def get_modalities(models=None):
	"""`get_modality_names` as the words the gateway and the engine read: each name lowercased."""
	return {
		model: {fieldname: [name.lower() for name in names] for fieldname, names in lists.items()}
		for model, lists in get_modality_names(models).items()
	}


def get_modality_names(models=None):
	"""{Model doc: {"input_modalities": [...], "output_modalities": [...]}}: the Modality records
	of each list in the order written, for `models` or for every Model. Both lists are there for
	every doc named."""
	filters = {"parenttype": "Model"}
	if models is not None:
		filters["parent"] = ("in", list(models))
	rows = frappe.get_all(
		"Model Modality Row", filters=filters, fields=["parent", "parentfield", "modality"], order_by="idx"
	)
	modalities = {model: {fieldname: [] for fieldname in MODALITY_FIELDS} for model in models or ()}
	for row in rows:
		lists = modalities.setdefault(row.parent, {fieldname: [] for fieldname in MODALITY_FIELDS})
		lists[row.parentfield].append(row.modality)
	return modalities


def is_root_weights_file(path, suffix):
	"""At the root, not in a subfolder. The listing is already non-recursive, so this is a second
	line of defence: counting a subfolder's quantizations bills the same model twice."""
	return path.endswith(suffix) and "/" not in path


def mirror_server(model):
	"""A box that already has the weights cached, or can pull them onto the disk sized for
	them."""
	rows = frappe.get_all(
		"Model Replica",
		filters={"model": model, "status": "Active"},
		fields=["inference_server"],
		limit=1,
	)
	return rows[0].inference_server if rows else None


@failure.reports_failure(doctype="Model")
def mirror_weights_to_s3(model):
	"""Worker: run mirror_weights.yml on the box, then stamp weights_s3_uri on success so new
	deploys pick the mirror up."""
	doc = frappe.get_doc("Model", model)
	settings = frappe.get_single("Grove Settings")
	inf = frappe.get_doc("Inference Server", mirror_server(model))
	uri = f"{settings.weights_bucket}/models/{doc.repo_id.replace('/', '--')}"
	play_name, rc = inf.run_playbook(
		"mirror_weights.yml",
		extravars={
			"vllm_home": inf.data_path,
			"vllm_hf_home": inf.hf_home,
			"vllm_hf_token": frappe.conf.get("hf_token", ""),
			"mirror_repo": doc.repo_id,
			"mirror_uri": uri,
			"mirror_env": settings.weights_s3_write_environment,
		},
		reference_doctype="Model",
		reference_docname=model,
	)
	if rc == 0:
		doc.db_set("weights_s3_uri", uri)
	return play_name, rc


def is_reachable(model, exclude=None, provider=None):
	"""True if a request for `model` (a doc) has somewhere to go: an Active Model Replica, a
	Running Pod, or a front on its own provider record.

	`exclude` drops one name, for on_trash where the row still exists during delete. `provider`
	is passed by a doc still being inserted: its row is not in the database yet."""
	filters = {"model": model, "status": "Active"}
	if exclude:
		filters["name"] = ("!=", exclude)
	if frappe.db.get_all("Model Replica", filters=filters, limit=1):
		return True
	if frappe.db.get_all("Pod", filters={"model": model, "status": "Running"}, limit=1):
		return True
	# A vendor model is reachable from the moment it exists. The key is unchecked because
	# validate refuses a provider holding an address without one.
	# TODO: clearing a provider's Base URL leaves its models published until something
	# touches them. They emit no route, so they 404 rather than mis-route.
	return has_vendor_front(provider or frappe.db.get_value("Model", model, "provider"))


def has_vendor_front(provider):
	"""Whether this provider record dials a vendor: the model's own geography, not any other's."""
	if not provider:
		return False
	fronts = frappe.db.get_value("Model Provider", provider, ["base_url", "anthropic_base_url"])
	return any(fronts or ())


# Read live off the Model, never mirrored onto a placement, so editing one reaches every placement
# on the next deploy.
LAUNCH_FIELDS = (
	"hf_repo", "weights_s3_uri", "enable_prefix_caching",
	"enable_auto_tool_choice", "tool_call_parser", "thinking", "reasoning_parser",
	"attention_heads", "weights_gb", "torch_dtype",
)


def config_dtype(config, shape=None):
	"""What vLLM will serve these weights in when nothing overrides it.

	Two spellings and two places: transformers renamed `torch_dtype` to `dtype` in 4.57, and a
	multimodal repo states it on the LANGUAGE model — Qwen3.5-4B has `text_config.dtype` and
	nothing above it. Missing it reads as "unknown" and silently skips the capability check."""
	for source in (shape or {}, config):
		if dtype := (source.get("torch_dtype") or source.get("dtype") or ""):
			return dtype
	return ""


def launch_config(model):
	"""This Model's intrinsic launch config as a plain mapping. Lives here rather than in
	grove/serving so that package stays frappe-free."""
	if not model:
		return {}
	config = frappe.db.get_value("Model", model, LAUNCH_FIELDS, as_dict=True)
	return {**config, **get_modalities([model])[model]} if config else {}


def sync_published(model, exclude=None):
	"""Unpublish a model nothing serves any more, after every placement status change. Never
	publishes: that is the operator's tick."""
	if model and frappe.db.exists("Model", model) and not is_reachable(model, exclude=exclude):
		frappe.db.set_value("Model", model, "published", 0)


def on_doctype_update():
	"""One doc per provider record per id: the race-safe arbiter behind the key."""
	frappe.db.add_unique("Model", ["provider", "model_id"], constraint_name="unique_provider_model")
