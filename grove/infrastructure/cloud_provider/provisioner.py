# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt
"""Standalone cloud Pod lifecycle via provider APIs. A Pod is self-contained: it holds its own
spawn spec and, for a serving pod, the vLLM config. Pods are NOT backed by a Machine — Machine +
Inference Server + Model Replica are the on-prem path.

The module-level functions at the bottom exist because frappe.enqueue resolves a dotted path to a
module function, not to a method."""

import time

import frappe
import requests

from grove import failure, log_relay
from grove.grove.doctype.pod_activity.pod_activity import record
from grove.infrastructure.cloud_provider.runpod import RunPodClient, RunPodError, pod_status
from grove.infrastructure.doctype.ssh_key.ssh_key import injected_public_keys
from grove.serving.base import DEFAULT_PORT

ENGINE_READY_TIMEOUT = 1500
ENGINE_POLL_INTERVAL = 15
SPAWN_RETRY_DELAY = 30
WARMUP_TIMEOUT = 120

VOLUME_MOUNT = "/data"

LOG_RECONNECT_DELAY = 2


class PodProvisioner:
	"""The provider side of one Pod. Construction touches nothing, so the decision helpers can be
	exercised without a site."""

	def __init__(self, pod, trigger="Manual"):
		self.pod = pod
		self.trigger = trigger
		self._client = None
		self._engine = None

	@property
	def engine(self):
		"""Built once per provisioner: a sync asks it four questions and each build reads the
		Engine Image and the Model."""
		if self._engine is None:
			self._engine = self.pod.engine
		return self._engine

	@property
	def client(self):
		if self._client is None:
			self._client = self.build_client()
		return self._client

	def build_client(self):
		"""Only RunPod for now — an unsupported provider fails here rather than part-way through a
		lifecycle call."""
		if not self.pod.cloud_provider:
			frappe.throw(f"Pod {self.pod.name} has no Cloud Provider.")
		provider = frappe.get_doc("Cloud Provider", self.pod.cloud_provider)
		if provider.provider_type != "runpod":
			frappe.throw(f"Unsupported provider: {provider.provider_type}")
		return provider.client()

	@property
	def public_keys(self):
		"""Missing keys stop a spawn up front: a pod nobody can log into is not worth billing
		for."""
		keys = injected_public_keys()
		if not keys:
			frappe.throw("No active SSH Key found — add one so Ansible/ops can reach the pod.")
		return keys

	@property
	def env(self):
		"""Whatever the engine needs, on the volume's paths so caches survive a restart, then the
		Pod's own Env rows on top. PUBLIC_KEY is added by get_config_kwargs. The attention backend is NOT
		here — it rides the serve command, because vLLM 0.24 dropped VLLM_ATTENTION_BACKEND."""
		pod = self.pod
		mount = pod.volume_mount_path or VOLUME_MOUNT
		env = self.engine.env(
			hf_home=f"{mount}/hf",
			cache_root=f"{mount}/vllm-cache",
			api_key=pod.get_password("api_key", raise_exception=False) or "",
			hf_token=frappe.conf.get("hf_token") or "",
			streaming_env=frappe.get_single("Grove Settings").weights_s3_engine_environment,
		)
		for row in pod.env or []:
			if row.key:
				env[row.key] = row.value or ""
		return env

	def register_registry_auth(self):
		"""RunPod takes no inline pull credentials: they are registered under a name and referenced
		by id. Re-registered on every spawn and restart so rotated credentials apply."""
		if not self.pod.engine_image:
			return None
		image = frappe.get_cached_doc("Engine Image", self.pod.engine_image)
		credentials = image.registry_credentials
		if not credentials:
			return None
		return self.client.register_registry_auth(f"grove-{frappe.scrub(image.image_provider)}", *credentials)

	def get_config_kwargs(self):
		"""What RunPod accepts on BOTH create and update, so a restart applies exactly what a spawn
		would. RunPod appends args to the image's entrypoint, so a custom image with neither runs
		as built."""
		pod = self.pod
		command = pod.serve_command or pod.startup_command or None
		return dict(
			volume_in_gb=pod.volume_in_gb or 50,
			container_disk_in_gb=pod.container_disk_gb or 20,
			ports=[f"{int(p.internal_port)}/{p.protocol or 'tcp'}" for p in pod.ports],
			env={"PUBLIC_KEY": self.public_keys, **self.env},
			image_name=pod.resolved_image,
			args=command or None,
			container_registry_auth_id=self.register_registry_auth(),
			volume_mount_path=pod.volume_mount_path or VOLUME_MOUNT,
			name=pod.name,
		)

	def get_spawn_kwargs(self):
		"""get_config_kwargs plus the create-only field update does not accept: the GPU shape."""
		if not self.pod.gpu_type_id:
			frappe.throw(
				f"Pod {self.pod.name} has no GPU Type ID — set it (provider GPU id, e.g. 'NVIDIA L40S')."
			)
		return dict(
			self.get_config_kwargs(),
			gpu_type_id=self.pod.gpu_type_id,
			gpu_count=self.pod.gpu_count or 1,
		)

	@property
	def engine_endpoint(self):
		"""Where the gateway reaches this pod's engine. An http row goes through the provider's HTTPS
		proxy: TLS terminates there and the address is keyed on the pod id rather than a mapping that
		moves every restart. A tcp row keeps its direct public_ip:external_port, which is plaintext
		and does move."""
		pod = self.pod
		serve_port = int(pod.serve_port or DEFAULT_PORT)
		row = next((p for p in pod.ports if int(p.internal_port) == serve_port), None)
		if not row:
			return ""
		if row.protocol == "http":
			return RunPodClient.proxy_url(pod.pod_id, serve_port) if pod.pod_id else ""
		if pod.public_ip and row.external_port:
			return f"http://{pod.public_ip}:{row.external_port}"
		return ""

	@property
	def health_path(self):
		"""Path polled to decide the container serves, "" when the provider's own state is the
		whole status. A custom image has to name its own — it takes just as long to come up, and
		without a gate the pod reads Running minutes before anything answers."""
		return self.pod.health_path or self.engine.health_path

	@property
	def is_warmup_due(self):
		"""Whether there is an engine here worth proving. One with no request cheap enough to shape
		says so by publishing none, and a bring-up that outran await_engine has nothing serving
		yet."""
		return bool(self.engine.warmup_request) and self.current_status == "Running"

	def run_warmup_request(self):
		"""Send the engine one real request: why it failed, "" when it served. Carries the bearer
		/health never needed: an unauthenticated POST is a 401 on a perfectly good engine running
		with VLLM_API_KEY."""
		request = self.engine.warmup_request
		if not request:
			return ""
		key = self.pod.get_password("api_key", raise_exception=False)
		try:
			response = requests.post(
				self.engine_endpoint + request["path"],
				json=request["body"],
				headers={"Authorization": f"Bearer {key}"} if key else {},
				timeout=WARMUP_TIMEOUT,
			)
		except requests.RequestException as error:
			return str(error)
		if response.status_code == 200:
			return ""
		return f"{response.status_code} {response.text[:200]}"

	def apply_provider_state(self, pod_api, running):
		"""For a gated pod, 'up' means the ENGINE answers, not just SSH — so engine_url is set only
		when Running, and the gateway never routes to a still-loading engine.

		`running` is the caller's verdict on whether the container is up (a bring-up forces it,
		since its snapshot predates the provider flipping to RUNNING). When it is not, the
		provider's own state decides — a pod still coming up reads Provisioning, not Stopped.
		Reloads first: the status writes around it go through db.set_value, which leaves the
		loaded doc stale."""
		self.pod = frappe.get_doc("Pod", self.pod.name)
		pod = self.pod
		state = "Running" if running else pod_status(pod_api.get("status"))
		port_map = pod_api.get("port_map") or {}
		for row in pod.ports:
			row.external_port = port_map.get(str(int(row.internal_port))) or 0
		if pod_api.get("public_ip"):
			pod.public_ip = pod_api["public_ip"]
		if pod_api.get("ssh_port"):
			pod.ssh_port = pod_api["ssh_port"]
			pod.ssh_user = "root"
		if not self.health_path:
			pod.status = state
		else:
			pod.status, pod.engine_url = self.gated_status(state)
		pod.save(ignore_permissions=True)
		frappe.db.commit()

	def gated_status(self, state):
		"""Status + route target for a gated pod. The provider saying RUNNING only means the
		container started, so this reads Running only when the engine answers. Every other
		provider state passes through as-is — a stopped pod is Stopped, not Loading."""
		if state != "Running":
			return state, ""
		url = self.engine_endpoint
		if url and _is_engine_serving(url + self.health_path):
			return "Running", url
		return "Loading", ""

	def await_engine(self, pod_api):
		"""Poll until the engine serves or ENGINE_READY_TIMEOUT passes, leaving it Loading for a
		later Sync. Ports do not move while the pod stays up, so the caller's snapshot holds."""
		deadline = time.time() + ENGINE_READY_TIMEOUT
		while time.time() < deadline:
			if frappe.db.get_value("Pod", self.pod.name, "status") == "Running":
				return
			time.sleep(ENGINE_POLL_INTERVAL)
			self.apply_provider_state(pod_api, running=True)

	def await_ready(self):
		"""The tail every bring-up shares."""
		ready = self.client.poll_pod_ready(self.pod.pod_id)
		self.apply_provider_state(ready, running=True)
		if self.health_path:
			self.await_engine(ready)
		if self.is_warmup_due and (error := self.run_warmup_request()):
			self.set_state({"status": "Loading", "engine_url": ""})
			failure.report("Pod", self.pod.name, "Pod engine failed warmup", error)
		self.sync_model_published()
		return ready

	def sync_model_published(self):
		"""Unpublish the Model when this pod was the last thing serving it — a serving pod has no
		Model Replica to drive that. Never publishes."""
		model = frappe.db.get_value("Pod", self.pod.name, "model")
		if not model:
			return
		from grove.grove.doctype.model.model import sync_published

		sync_published(model)

	def set_state(self, values):
		"""Status-ish writes go through db.set_value: cheap, and they skip validate so a
		lifecycle transition can't be blocked by an unrelated field."""
		frappe.db.set_value("Pod", self.pod.name, values)
		frappe.db.commit()

	def spawn(self):
		spawn_kwargs = self.get_spawn_kwargs()
		started = frappe.utils.now_datetime()
		self.set_state({"status": "Provisioning", "provision_attempts": 0})
		try:
			pod_api = self.create_provider_pod(spawn_kwargs)
			ready = self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Spawn", started)
		self.log("Spawn", "Success", f"{self.current_status} at {ready['public_ip']}", started=started)
		return {
			"outcome": "success",
			"pod_id": pod_api["pod_id"],
			"public_ip": ready["public_ip"],
		}

	def create_provider_pod(self, spawn_kwargs):
		"""The one call that is retried: the provider refusing to create the pod is the failure a
		later ask can fix. A pod that got an id is brought up exactly once."""
		retries = int(self.pod.provision_retries or 0)
		for attempt in range(1, retries + 2):
			self.set_state({"provision_attempts": attempt})
			try:
				pod_api = self.client.spawn_pod(**spawn_kwargs)
			except RunPodError as error:
				self.log("Spawn", "Failure", error, attempt=attempt)
				if attempt > retries:
					raise
				time.sleep(SPAWN_RETRY_DELAY)
				continue
			self.set_state({"pod_id": pod_api["pod_id"]})
			return pod_api

	def log(self, event, outcome, detail="", attempt=None, started=None):
		record(self.pod.name, event, outcome, detail, attempt=attempt, started=started, trigger=self.trigger)

	def sync(self):
		"""The route table follows on the next projection tick."""
		self.require_pod_id("spawn it first")
		try:
			pod_api = self.client.get_pod(self.pod.pod_id)
		except RunPodError as e:
			if "404" not in str(e):
				raise
			self.mark_terminated("Gone on the provider — terminated outside Grove")
			return {"status": "Terminated"}
		running = pod_status(pod_api.get("status")) == "Running" and bool(pod_api.get("public_ip"))
		self.apply_provider_state(pod_api, running)
		self.sync_model_published()
		return {"status": self.current_status}

	def restart(self):
		"""Apply edited config to the live pod IN PLACE: RunPod's update takes the container
		start command, and the pod resets to pick it up. The pod id and the volume survive, so the
		weights under HF_HOME are not re-downloaded — the reason this is not a respawn.
		restart_blocker is re-checked here as well as at enqueue time, since the pod's state can
		move while the job waits."""
		if not self.pod.pod_id:
			return self.spawn()
		if reason := self.restart_blocker(self.client.get_pod(self.pod.pod_id)):
			frappe.throw(reason)

		config_kwargs = self.get_config_kwargs()
		started = frappe.utils.now_datetime()
		try:
			self.set_state({"status": "Provisioning", "engine_url": ""})
			self.sync_model_published()

			self.client.update_pod(self.pod.pod_id, **config_kwargs)
			# RunPod re-draws the port map on a reset, so endpoints are re-read, not assumed.
			self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Restart", started)
		self.log("Restart", "Success", started=started)
		return {"status": self.current_status, "pod_id": self.pod.pod_id}

	def stop(self):
		"""Frees the GPU but keeps the pod and its volume, so weights survive."""
		self.require_pod_id("nothing to stop")
		try:
			self.client.stop_pod(self.pod.pod_id)
		except RunPodError as e:
			return self.fail(e, "Stop")
		self.set_state({"status": "Stopped", "engine_url": ""})
		self.sync_model_published()
		self.log("Stop", "Success")
		return {"status": "Stopped"}

	def start(self):
		"""Endpoints are re-read: the provider re-maps ports on start, so the old external ports
		are stale."""
		self.require_pod_id("spawn it first")
		started = frappe.utils.now_datetime()
		try:
			self.client.start_pod(self.pod.pod_id)
			self.set_state({"status": "Provisioning"})
			self.await_ready()
		except RunPodError as e:
			return self.fail(e, "Start", started)
		self.log("Start", "Success", started=started)
		return {"status": self.current_status}

	def terminate(self):
		"""The tick prunes its route on the next pass."""
		if not self.pod.pod_id:
			return {"outcome": "error", "message": "No provider pod to terminate"}
		try:
			self.client.terminate_pod(self.pod.pod_id)
		except RunPodError as e:
			return self.fail(e, "Terminate")
		self.mark_terminated(f"Provider pod {self.pod.pod_id} terminated")
		return {"outcome": "success"}

	def mark_terminated(self, detail):
		"""The one write for a pod that is gone, whoever removed it."""
		self.set_state({"pod_id": "", "status": "Terminated", "engine_url": ""})
		self.sync_model_published()
		self.log("Terminate", "Success", detail)

	def restart_blocker(self, live):
		"""Why this Pod's config cannot be applied to its live provider pod, or None when it
		can. `live` is the parsed provider pod (see RunPodClient._parse_pod).

		Restart is deliberately non-destructive. Moving GPUs is the case that matters: a respawn
		re-downloads the weights, which is too expensive to do off a button labelled Restart, so
		it has to be an explicit Terminate then Spawn."""
		move = (
			"RunPod cannot move a pod's GPUs, so this needs an explicit Terminate, then "
			"Spawn — which re-downloads the weights."
		)
		if live.get("status") != "RUNNING":
			return (
				f"Pod {self.pod.name} is not running, and an update only resets a live "
				"container. Start it first, then Restart to apply this config."
			)
		if live.get("gpu_count") and (self.pod.gpu_count or 1) != live["gpu_count"]:
			return f"GPU count changed ({live['gpu_count']} → {self.pod.gpu_count or 1}). {move}"
		if live.get("gpu_type_id") and self.pod.gpu_type_id != live["gpu_type_id"]:
			return f"GPU type changed ({live['gpu_type_id']} → {self.pod.gpu_type_id}). {move}"
		return None

	def validate_restart(self):
		"""Called before the job is enqueued, so the operator gets the reason in the form instead
		of a failed background job."""
		if not self.pod.pod_id:
			return
		live = self.client.get_pod(self.pod.pod_id)
		if reason := self.restart_blocker(live):
			frappe.throw(reason)

	def require_pod_id(self, remedy):
		if not self.pod.pod_id:
			frappe.throw(f"Pod {self.pod.name} has no provider pod id — {remedy}.")

	@property
	def current_status(self):
		"""The Pod's status as stored, not as loaded — the lifecycle calls write through
		db.set_value and apply_provider_state."""
		return frappe.db.get_value("Pod", self.pod.name, "status")

	def fail(self, error, event, started=None):
		"""Hands the message back instead of raising, since these run as background jobs, and
		writes the call's terminal Pod Activity row.

		Only a lifecycle that never got a provider pod is parked Stopped. Once there is a pod id
		the failure says nothing about the container, which is very likely still coming up.
		Calling that Stopped drops its gateway route, so the status is left for the scheduled
		reconcile to read off the provider."""
		title = f"Pod {event.lower()} failed {self.pod.name}"
		if not frappe.db.get_value("Pod", self.pod.name, "pod_id"):
			self.set_state({"status": "Stopped"})
		frappe.log_error(title=title)
		# Not @reports_failure: this path never raises, and a Pod has no Broken status to mark.
		failure.report("Pod", self.pod.name, title, str(error))
		self.log(event, "Failure", error, started=started)
		return {"outcome": "error", "message": str(error)}


def _is_engine_serving(health_url, timeout=5):
	"""This — not SSH-reachability — is real readiness; weights can take many minutes to download
	+ load after the pod is up. vLLM's /health needs no api-key."""
	try:
		return requests.get(health_url, timeout=timeout).status_code == 200
	except requests.RequestException:
		return False


def _provisioner(pod_name, trigger="Manual"):
	return PodProvisioner(frappe.get_doc("Pod", pod_name), trigger=trigger)


def spawn_pod_doc(pod_name, trigger="Manual"):
	return _provisioner(pod_name, trigger).spawn()


def sync_pod(pod_name):
	return _provisioner(pod_name).sync()


def restart_pod(pod_name):
	return _provisioner(pod_name).restart()


def stop_pod(pod_name):
	return _provisioner(pod_name).stop()


def start_pod(pod_name):
	return _provisioner(pod_name).start()


def terminate_pod_doc(pod_name, trigger="Manual"):
	return _provisioner(pod_name, trigger).terminate()


def pod_log_events(client, pod_id, tail=100):
	"""The pod's log lines off the provider, reconnecting from the last event id each time the
	stream drops — it ends on its own whenever the pod goes quiet. Yields None on a keep-alive
	so the relay still ticks on a silent pod, and gives up after three instant empty
	reconnects: that means the pod has stopped producing logs (terminated) entirely."""
	last_id = None
	dead_rounds = 0
	try:
		while dead_rounds < 3:
			opened_at = time.monotonic()
			received = 0
			for event_id, event in client.stream_logs(pod_id, tail=tail, last_event_id=last_id):
				if not event:
					yield None
					continue
				last_id = event_id or last_id
				received += 1
				yield event.get("line", "")
			closed_empty_at_once = not received and time.monotonic() - opened_at < 5
			dead_rounds = dead_rounds + 1 if closed_empty_at_once else 0
			tail = 0
			time.sleep(LOG_RECONNECT_DELAY)
			yield None  # tick, so a Stop pressed during a reconnect lands right away
	except RunPodError as error:
		yield f"— {error} —"


def stream_pod_logs(pod_name, tail=100):
	pod = frappe.get_doc("Pod", pod_name)
	client = PodProvisioner(pod).client
	log_relay.relay(pod_log_events(client, pod.pod_id, tail), "Pod", pod_name)
